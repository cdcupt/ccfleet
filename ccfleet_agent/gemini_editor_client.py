"""Interactive terminal editor for one paired CCFleet Antigravity slot."""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import re
import secrets
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import unicodedata
from pathlib import Path

MAX_FRAME = 4 * 1024 * 1024
MAX_PROMPT = 64 * 1024
REQUEST_FIELDS = {
    "read": {"path"}, "list": {"path"}, "mkdir": {"path"},
    "approve": {"path", "content"}, "write": {"path", "content"}, "test": set(),
    "shell": {"command"}, "publish": {"request"},
}


def terminal_text(text):
    """Display untrusted model/file text without letting it drive the terminal."""
    def visible(c):
        if c in "\n\t" or not unicodedata.category(c).startswith("C"):
            return c
        return ascii(c)[1:-1]
    return "".join(map(visible, text))


def confirm(question):
    if sys.stdin.isatty():
        # A pasted multiline prompt must not accidentally supply a later approval.
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    try:
        return input(question + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def show_patch(path, before, after):
    def lines(text):
        result = text.split("\n")
        if result[-1] == "":
            return result[:-1]
        return [*result, "\\ No newline at end of file"]
    for line in difflib.unified_diff(lines(before), lines(after), "a/" + path, "b/" + path,
                                     lineterm=""):
        print(terminal_text(line))


class ApprovalInterrupted(EOFError):
    """The connection ended while the user was deciding on a local action."""


class SlotError(ValueError):
    """A well-formed error frame from the paired slot."""


def request_operation(message):
    op = message.get("op")
    fields = REQUEST_FIELDS.get(op) if isinstance(op, str) else None
    if fields is None or set(message) != {"type", "id", "op"} | fields:
        raise ValueError("invalid editor operation")
    if op == "publish":
        request = message["request"]
        if not isinstance(request, dict) or request.get("action") not in ("push", "create_review"):
            raise ValueError("invalid repository action")
    elif any(not isinstance(message[k], str) for k in fields):
        raise ValueError("invalid editor operation arguments")
    return op


class ProjectCommands:
    """Local authority for the duration of one explicitly negotiated model turn."""

    def __init__(self, tools, files, project, fleet_home):
        self.tools = tools
        self.access = files.ProjectAccess(project, fleet_home)
        self.capable = False
        self.grant = None

    def connected(self, ready):
        caps = ready.get("capabilities")
        self.capable = isinstance(caps, dict) and caps.get("projectShell") is True

    @contextlib.contextmanager
    def turn(self, frame):
        # A grant revoked and then recreated belongs to a later user decision.
        # Queued actions from this turn may only use its original generation.
        self.grant = self.access.snapshot() if self.capable else None
        try:
            if self.grant is not None:
                frame["projectShell"] = True
            yield
        finally:
            self.grant = None

    def allowed(self):
        return self.grant is not None and self.grant == self.access.snapshot()

    def title(self, msg):
        if not self.allowed():
            raise ValueError("project commands are disabled for this turn")
        if msg["op"] == "shell":
            command = msg["command"]
            if not command.strip() or "\x00" in command or len(command.encode()) > 16 * 1024:
                raise ValueError("invalid local command")
            text = terminal_text(command).replace("\n", "\\n").replace("\t", "\\t")
            return "Run project command: " + text
        if msg["request"]["action"] == "push":
            return "Push to the project's authorized remote"
        return "Create a review on the project's authorized remote"

    def execute(self, msg, cancelled):
        def interrupted():
            return cancelled() or not self.allowed()

        if interrupted():
            raise ValueError("project command unavailable")
        if msg["op"] == "shell":
            return self.tools.run_local_command(self.access, msg["command"], cancelled=interrupted)
        return self.tools.publish_project(self.access, msg["request"], cancelled=interrupted)


class Editor:
    def __init__(self, project, tools, files, test=None, *, fleet_home=None):
        self.tools = tools
        fleet_home = fleet_home or Path.home() / ".config" / "ccfleet"
        private = tools.PrivateFolders(fleet_home)
        self.workspace = tools.Workspace(project, self._confirm_workspace, test, file_tools=files,
                                         private_folders=private, cancelled=self.disconnected)
        self.commands = ProjectCommands(tools, files, self.workspace.path, fleet_home)
        self.channel = None
        self.seen = {}
        self.approval = None

    def disconnected(self):
        return self.channel is not None and self.channel.disconnected()

    def answer(self, question):
        # Keep the tty in canonical mode so unfinished or pasted approval input
        # can be flushed when the pinned SSH connection closes.
        if self.channel is None or not sys.stdin.isatty():
            return input(question)
        fd = sys.stdin.fileno()
        termios.tcflush(fd, termios.TCIFLUSH)
        print(question, end="", flush=True)
        while not self.disconnected():
            if select.select([fd], [], [], 0.15)[0]:
                answer = sys.stdin.readline()
                if not answer:
                    raise EOFError
                if not self.disconnected():
                    return answer.rstrip("\n")
        termios.tcflush(fd, termios.TCIFLUSH)
        print()
        raise ApprovalInterrupted

    def confirm(self, question):
        if self.channel is None or not sys.stdin.isatty():
            approved = confirm(question)
        else:
            approved = self.answer(question + " [y/N] ").strip().lower() in ("y", "yes")
        if self.disconnected():
            raise ApprovalInterrupted
        return approved

    def _confirm_workspace(self, action, detail):
        if action == "Save file":
            # The immediately enclosing write verifies this approval and its content.
            return (not self.disconnected() and self.approval is not None
                    and self.approval[0] == detail
                    and (not self.approval[3] or self.commands.allowed()))
        if action in ("Create directory", "Run local tests"):
            if self.commands.allowed() and not self.disconnected():
                print(f"{action} on this Mac ({terminal_text(detail)})", flush=True)
                return True
            return self.confirm(f"{action} on this Mac ({terminal_text(detail)})?")
        return False

    def _approved_edit(self, path, content):
        self.approval = None
        if len(content.encode("utf-8")) > self.tools.LIMIT:
            raise ValueError("file too large for an editor operation")
        current = self.workspace.read_optional(path)
        expected = current["sha256"] if current is not None else "new"
        if expected != self.seen.get(path, "new"):
            raise ValueError("the file must be read again before editing")
        show_patch(path, current["text"] if current else "", content)
        action = "Edit" if current else "Create"
        automatic = self.commands.allowed()
        if not automatic and not self.confirm(f"{action} {terminal_text(path)} on this Mac?"):
            return False
        self.approval = (path, hashlib.sha256(content.encode()).hexdigest(), expected, automatic)
        return True

    def _write(self, path, content):
        approved = self.approval
        try:
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if not approved or (path, digest) != approved[:2]:
                raise ValueError("this change needs local approval")
            self.workspace.call("save_project", {"path": path, "text": content,
                                                 "expected": approved[2]})
            self.seen[path] = digest
            print(f"Saved {terminal_text(path)}.", flush=True)
            return True
        finally:
            self.approval = None

    def handle(self, request):
        op = request_operation(request)
        if op in ("shell", "publish"):
            print(self.commands.title(request), flush=True)
            try:
                result = self.commands.execute(request, self.disconnected)
            except (OSError, ValueError, subprocess.SubprocessError):
                print("Local operation failed.", flush=True)
                raise
            print(terminal_text(result["output"]), end="")
            print(f"\nLocal operation exited with {result['exit']}.", flush=True)
            return result
        if op == "read":
            path = request["path"]
            item = self.workspace.call("read_project", {"path": path})
            if path not in self.seen and len(self.seen) >= 10000:
                # Eviction simply requires older files to be reread before another edit.
                self.seen.pop(next(iter(self.seen)))
            self.seen[path] = item["sha256"]
            return item["text"]
        if op == "approve":
            return self._approved_edit(request["path"], request["content"])
        if op == "write":
            return self._write(request["path"], request["content"])
        if op == "list":
            return self.workspace.call("list_project", {"path": request["path"]})
        if op == "mkdir":
            self.workspace.call("make_project_directory", {"path": request["path"]})
            return True
        if self.workspace.command is None:
            raise ValueError("tests are disabled; the user can enable a runner with /test")
        if self.workspace.test_directory() is None:
            folder = self.answer("Which project subfolder should run the tests? "
                                 "(blank to skip) ").strip()
            if not folder:
                raise ValueError("local test folder was not selected")
            folder = str(Path(folder))
            if self.workspace.test_directory(folder) is None:
                raise ValueError("choose a project subfolder for local tests")
            self.workspace.test_relative = folder
        result = self.workspace.call("test_project", {})
        print(terminal_text(result["output"]), end="")
        print(f"\nLocal tests exited with {result['exit']}.", flush=True)
        return result

    def test_command(self, prompt):
        name, _, folder = prompt.removeprefix("/test").strip().partition(" ")
        if not name:
            active = " ".join(self.workspace.command) if self.workspace.command else "off"
            print(f"Current local test command: {active}. Use /test "
                  + "|".join((*self.tools.COMMANDS, "off")) + " [project subfolder].")
        elif name == "off" and not folder:
            self.workspace.command = None
            self.workspace.test_relative = "."
            print("Local tests disabled.")
        elif name in self.tools.COMMANDS:
            folder = str(Path(folder.strip() or "."))
            if folder != ".":
                try:
                    if self.workspace.test_directory(folder) is None:
                        raise ValueError("choose a project subfolder")
                except (OSError, ValueError):
                    print("Choose an existing project subfolder for local tests.")
                    return
            self.workspace.test_relative = folder
            self.workspace.command = self.tools.COMMANDS[name]
            print("Local tests will ask before running " + " ".join(self.workspace.command) + ".")
        else:
            print("Unknown test runner. Use /test to list available commands.")


class Channel:
    def __init__(self, pipe):
        self.pipe = pipe

    def disconnected(self):
        if self.pipe.poll() is not None:
            return True
        poller = select.poll()
        poller.register(self.pipe.stdout, select.POLLHUP | select.POLLERR)
        return bool(poller.poll(0))

    def send(self, message):
        raw = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > MAX_FRAME:
            raise ValueError("editor message exceeds the size limit")
        self.pipe.stdin.write(struct.pack(">I", len(raw)) + raw)
        self.pipe.stdin.flush()

    def _read(self, count, deadline):
        data = bytearray()
        while len(data) < count:
            wait = max(0, deadline - time.monotonic())
            if not select.select([self.pipe.stdout], [], [], wait)[0]:
                raise TimeoutError("the Gemini slot stopped responding")
            part = os.read(self.pipe.stdout.fileno(), count - len(data))
            if not part:
                raise EOFError
            data.extend(part)
        return bytes(data)

    def receive(self, timeout=920):
        deadline = time.monotonic() + timeout
        size, = struct.unpack(">I", self._read(4, deadline))
        if not 0 < size <= MAX_FRAME:
            raise ValueError("invalid Gemini editor frame size")
        try:
            message = json.loads(self._read(size, deadline).decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise ValueError("invalid Gemini editor message") from exc
        if not isinstance(message, dict):
            raise ValueError("invalid Gemini editor message")
        if message.get("type") == "error":
            reason = message.get("message")
            raise SlotError(terminal_text(reason[:2048]) if isinstance(reason, str)
                             else "the Gemini slot could not complete this session")
        return message


def turn(channel, editor, prompt, *, number=None, progress=False):
    request = {"type": "prompt", "text": prompt}
    if number is not None:
        request["turn"] = number
    editor.approval = None
    try:
        with editor.commands.turn(request):
            channel.send(request)
            receive_turn(channel, editor, number, progress)
    finally:
        editor.approval = None


def receive_turn(channel, editor, number, progress):
    ids = set()
    while True:
        message = channel.receive()
        kind = message.get("type")
        if kind == "progress" or kind == "end" and number is not None:
            if type(message.get("turn")) is not int or message["turn"] != number:
                raise ValueError("unexpected Gemini turn number")
            if kind == "progress":
                if not progress or set(message) != {"type", "turn"}:
                    raise ValueError("unexpected Gemini progress frame")
                continue
        if kind == "end" and isinstance(message.get("reason"), str):
            reason = message["reason"]
            if reason == "end_turn":
                print()
            else:
                # Unknown provider reasons must never render untrusted diagnostics.
                reasons = {"cancelled": "cancelled", "refusal": "request declined",
                           "refused": "request declined", "max_tokens": "token limit reached",
                           "max_turn_requests": "request limit reached"}
                print(f"\nGemini stopped this turn ({reasons.get(reason, 'interrupted')}). "
                      "You can send another message.")
            return
        if kind == "text" and isinstance(message.get("text"), str):
            print(terminal_text(message["text"]), end="", flush=True)
            continue
        request_id = message.get("id")
        if (kind != "request" or type(request_id) is not int or request_id <= 0
                or request_id in ids or len(ids) >= 4096):
            raise ValueError("unexpected Gemini editor message")
        ids.add(request_id)
        reply = {"type": "response", "id": request_id}
        if editor.disconnected():
            raise EOFError
        try:
            reply["result"] = editor.handle(message)
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            reply["error"] = "local file or test runner unavailable"
        except ValueError as exc:
            reply["error"] = str(exc)[:512]
        channel.send(reply)


COMMAND_HELP = """Gemini commands
  /help                      Show these commands
  /model                     Show the current model and open the model picker
  /model <number or ID>      Select a model for this conversation
  /test                      Show the current test command
  /test <runner> [subfolder] Choose pytest, npm, go or cargo for local tests
  /test off                  Disable local tests
  /reconnect                 Rejoin the previous conversation, or start fresh after expiry
  /exit or /quit             Close this session

To enable project commands, launch ccfleet gemini --full-access in a local terminal.
To revoke access in that project, run ccfleet gemini --revoke-full-access.
Type a message to talk to Gemini."""
MODEL_ID = re.compile(r"gemini-[a-z0-9.-]{1,80}\Z")


def model_command(channel, current, models, choice):
    if models is None:
        print(f"Current model: {terminal_text(current)}")
        print("Update the private Mac slot service to enable model selection.")
        return current
    if not choice:
        print("Available Gemini models")
        for number, model in enumerate(models, 1):
            selected = " (current)" if model == current else ""
            print(f"  {number}. {model}{selected}")
        try:
            choice = input("Model number or ID (Enter to keep): ").strip()
        except EOFError:
            return current
        if not choice:
            return current
    target = next((model for index, model in enumerate(models, 1) if choice == str(index)), choice)
    if target not in models:
        print("Choose a listed model ID or number. Use /model to see the available models.")
        return current
    if target == current:
        print(f"Using {current}.")
        return current
    channel.send({"type": "set_model", "model": target})
    reply = channel.receive()
    active = reply.get("model")
    if reply.get("type") != "model" or active not in models:
        raise ValueError("the slot did not confirm the active Gemini model")
    if "error" in reply or active != target:
        print(f"The slot could not apply the selection. Using {active}.")
    else:
        print(f"Switched to {active}.")
    return active


SLOT_BUSY = "Another Gemini editor is using this slot. Close it and reconnect."
MISSING = "The previous Gemini session cannot be resumed."
FRESH = "Send /reconnect to start a fresh Gemini conversation in this terminal."
INTERRUPTED_ERRORS = {
    "The Gemini editor session timed out. Reopen it to continue.",
    "The Gemini slot timed out. Reopen the editor to continue.",
    "The Google editor agent stopped on the slot.",
    "The Google editor could not complete this request.",
    "The Gemini editor is unavailable on the private Mac. Reopen it to try again.",
}


def ready_session(ready, token=None):
    current = ready.get("model")
    if (ready.get("type") != "ready" or not isinstance(current, str)
            or not MODEL_ID.fullmatch(current)):
        raise ValueError("the private slot did not open a Gemini editor session")
    models = ready.get("models")
    if "models" in ready:
        if (not isinstance(models, list) or not 1 <= len(models) <= 256 or current not in models
                or any(not isinstance(m, str) or not MODEL_ID.fullmatch(m) for m in models)
                or len(set(models)) != len(models)):
            raise ValueError("the private slot returned an invalid Gemini model list")
    caps = ready.get("capabilities")
    caps = caps if isinstance(caps, dict) else {}
    offered = None
    if caps.get("resumeSession") is True:
        offered = ready.get("resumeToken")
        if not isinstance(offered, str) or not re.fullmatch(r"[0-9a-f]{64}", offered):
            raise ValueError("invalid Gemini recovery capability")
    if token is not None:
        if (ready.get("resumed") is not True or offered is None
                or not secrets.compare_digest(token, offered)):
            raise ValueError("the slot did not confirm the previous Gemini session")
    elif ready.get("resumed") is True:
        raise ValueError("unexpected Gemini recovery reply")
    return current, models, offered, caps.get("cancelTurn") is True


class TerminalSession:
    def __init__(self, editor, open_transport, stack):
        self.editor = editor
        self.open_transport = open_transport
        self.stack = stack
        self.channel = None
        self.current = self.models = None
        self.offered = self.token = None
        self.tagged = False
        self.number = 0

    def detach(self):
        self.channel = self.editor.channel = None
        self.editor.commands.capable = False
        self.offered = None
        self.editor.approval = None
        self.editor.seen.clear()
        self.stack.close()

    def connect(self):
        token = self.token
        channel = self.stack.enter_context(self.open_transport())
        message = {"type": "open", "version": 1} if token is None else {
            "type": "resume", "version": 1, "resumeToken": token,
        }
        channel.send(message)
        ready = channel.receive(timeout=420)
        current, models, offered, tagged = ready_session(ready, token)
        if token is not None:
            if current != self.current:
                raise ValueError("the restored session did not confirm the previous Gemini model")
            channel.send({"type": "enableResume"})
        self.current, self.models = current, models
        self.tagged = tagged
        self.editor.commands.connected(ready)
        self.channel, self.offered = channel, offered
        self.editor.channel = channel

    def reconnect(self, *, fresh):
        if self.token is None and not fresh:
            print(FRESH + " Your message was not sent.")
            return False
        deadline = time.monotonic() + 10
        delay = 0.4
        while True:
            try:
                self.connect()
                break
            except SlotError as exc:
                self.detach()
                # The prior worker may still be detaching. Admission was explicitly
                # refused, so no new prompt has been sent or needs to be replayed.
                if (self.token and str(exc) == SLOT_BUSY
                        and time.monotonic() + delay <= deadline):
                    time.sleep(delay)
                    delay = min(delay * 2, 1.5)
                    continue
                if str(exc) == MISSING:
                    self.token = None
                    print(MISSING, FRESH)
                else:
                    print("The slot could not reconnect. Check slot access and try /reconnect.")
                return False
            except (EOFError, OSError, subprocess.TimeoutExpired):
                self.detach()
                print("Could not reach the slot. Try /reconnect. Your message was not sent.")
                return False
        if self.token:
            print("Reconnected to the previous Gemini conversation.")
        else:
            print("Connected to a fresh Gemini conversation in this terminal.")
        return True

    def send_prompt(self, prompt):
        if self.offered and not self.token:
            self.token = self.offered
            self.channel.send({"type": "enableResume"})
        self.number += 1
        turn(self.channel, self.editor, prompt, number=self.number if self.tagged else None,
             progress=self.token is not None and self.tagged)


def session(connection):
    editor = connection.editor
    connection.connect()
    project = terminal_text(str(editor.workspace.path))
    print(f"Gemini · {terminal_text(connection.current)} · {project}")
    if editor.commands.access.enabled():
        if editor.commands.capable:
            print("Project access enabled. Gemini can edit files and run local project commands. "
                  "Type /help for controls.")
        else:
            print("Update the private Mac slot to use project commands. Edits and local tests "
                  "ask for approval in this session. Type /help for controls.")
    else:
        print("Gemini reads requested project files through your slot. Edits and local tests "
              "ask for approval. Type /help for commands.")
    stale_answer = False
    while True:
        try:
            prompt = input("\nYou › ").strip()
        except EOFError:
            prompt = "/exit"
        if not prompt:
            continue
        parts = prompt.split(maxsplit=1)
        command = parts[0]
        argument = parts[1] if len(parts) == 2 else ""
        if stale_answer:
            stale_answer = False
            if prompt.lower() in ("y", "yes", "n", "no"):
                print("That approval expired. Enter a new instruction.")
                continue
        if command in ("/exit", "/quit"):
            if argument:
                print(f"Use {command} by itself to disconnect.")
                continue
            return
        if command == "/help":
            print(COMMAND_HELP)
            continue
        if command == "/test":
            editor.test_command("/test " + argument)
            continue
        if command == "/reconnect" and argument:
            print("Use /reconnect by itself.")
            continue
        if command not in ("/model", "/reconnect"):
            if re.fullmatch(r"/[a-zA-Z][a-zA-Z0-9_-]*", command):
                print(f"Unknown command {command}. Type /help for the available commands.")
                continue
            if len(prompt.encode("utf-8")) > MAX_PROMPT:
                print("The message is too long. Please shorten it.")
                continue
        if connection.channel is not None and editor.disconnected():
            connection.detach()
        if command == "/reconnect":
            if connection.channel is None:
                connection.reconnect(fresh=True)
            else:
                print("The Gemini connection is already open.")
            continue
        if connection.channel is None and not connection.reconnect(fresh=False):
            continue
        try:
            if command == "/model":
                connection.current = model_command(connection.channel, connection.current,
                                                   connection.models, argument)
            else:
                connection.send_prompt(prompt)
        except (EOFError, OSError, subprocess.TimeoutExpired, SlotError) as exc:
            if isinstance(exc, SlotError) and str(exc) not in INTERRUPTED_ERRORS:
                raise
            stale_answer = isinstance(exc, ApprovalInterrupted)
            connection.detach()
            print("\nThe connection to Gemini was interrupted. The previous request may have "
                  "partly completed and will not be replayed.")
            print("Your next instruction will try to resume the conversation." if connection.token
                  else FRESH)


@contextlib.contextmanager
def transport(command, environment):
    proc = None
    try:
        try:
            proc = subprocess.Popen(command, cwd="/", env=environment, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
        except OSError as exc:
            raise OSError("could not start the CCFleet connection; check the local "
                          "OpenSSH installation") from exc
        yield Channel(proc)
    finally:
        if proc is not None:
            with contextlib.suppress(BrokenPipeError):
                proc.stdin.close()
            if proc.poll() is None:
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            proc.stdout.close()


def run(project, command, environment, tools, files, test=None, *, fleet_home=None):
    editor = Editor(project, tools, files, test, fleet_home=fleet_home)
    try:
        with contextlib.ExitStack() as stack:
            connection = TerminalSession(editor, lambda: transport(command, environment), stack)
            try:
                session(connection)
            finally:
                if connection.channel is not None:
                    with contextlib.suppress(OSError, ValueError):
                        connection.channel.send({"type": "close"})
    except EOFError as exc:
        raise ValueError("the Gemini editor connection closed; check the private slot's "
                         "editor service and Antigravity sign-in before reconnecting") from exc
    except OSError as exc:
        raise ValueError(str(exc)) from exc
    finally:
        editor.workspace.close()
