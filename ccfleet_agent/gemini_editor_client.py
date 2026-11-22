"""Interactive terminal editor for one paired CCFleet Antigravity slot."""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import re
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


class Editor:
    def __init__(self, project, tools, files, test=None, *, fleet_home=None):
        self.tools = tools
        private = tools.PrivateFolders(fleet_home or Path.home() / ".config" / "ccfleet")
        self.workspace = tools.Workspace(project, self._confirm_workspace, test, file_tools=files,
                                         private_folders=private)
        self.seen = {}
        self.approval = None

    def _confirm_workspace(self, action, detail):
        if action == "Save file":
            # The immediately enclosing write verifies this approval and its content.
            return self.approval is not None and self.approval[0] == detail
        if action in ("Create directory", "Run local tests"):
            return confirm(f"{action} on this Mac ({terminal_text(detail)})?")
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
        if not confirm(f"{'Edit' if current else 'Create'} {terminal_text(path)} on this Mac?"):
            return False
        self.approval = (path, hashlib.sha256(content.encode("utf-8")).hexdigest(), expected)
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
        op = request.get("op")
        fields = REQUEST_FIELDS.get(op, set()) if isinstance(op, str) else set()
        if (not isinstance(op, str) or op not in REQUEST_FIELDS
                or set(request) != {"type", "id", "op"} | fields
                or any(not isinstance(request[field], str) for field in fields)):
            raise ValueError("invalid editor operation")
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
            folder = input("Which project subfolder should run the tests? (blank to skip) ").strip()
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
            raise ValueError(terminal_text(reason[:2048]) if isinstance(reason, str)
                             else "the Gemini slot could not complete this session")
        return message


def turn(channel, editor, prompt):
    channel.send({"type": "prompt", "text": prompt})
    ids = set()
    editor.approval = None
    while True:
        message = channel.receive()
        kind = message.get("type")
        if kind == "end" and isinstance(message.get("reason"), str):
            editor.approval = None
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
        number = message.get("id")
        if (kind != "request" or type(number) is not int or number <= 0
                or number in ids or len(ids) >= 4096):
            raise ValueError("unexpected Gemini editor message")
        ids.add(number)
        reply = {"type": "response", "id": number}
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
  /exit or /quit             Close this session

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


def session(channel, editor):
    channel.send({"type": "open", "version": 1})
    # A cold slot verifies the registry, downloads the agent and authenticates the session.
    ready = channel.receive(timeout=420)
    if ready.get("type") != "ready" or not isinstance(ready.get("model"), str):
        raise ValueError("the private slot did not open a Gemini editor session")
    current = ready["model"]
    models = ready.get("models")
    if models is not None and (not isinstance(models, list) or current not in models
                               or any(not isinstance(m, str) or not MODEL_ID.fullmatch(m)
                                      for m in models) or len(set(models)) != len(models)):
        raise ValueError("the private slot returned an invalid Gemini model list")
    print(f"Gemini · {terminal_text(current)} · {terminal_text(str(editor.workspace.path))}")
    print("Gemini reads requested project files through your slot. Edits and local tests "
          "ask for approval. Type /help for commands.")
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
        if command in ("/exit", "/quit"):
            if argument:
                print(f"Use {command} by itself to disconnect.")
                continue
            channel.send({"type": "close"})
            return
        if command == "/help":
            print(COMMAND_HELP)
        elif command == "/model":
            current = model_command(channel, current, models, argument)
        elif command == "/test":
            editor.test_command("/test " + argument)
        elif re.fullmatch(r"/[a-zA-Z][a-zA-Z0-9_-]*", command):
            print(f"Unknown command {command}. Type /help for the available commands.")
        else:
            if len(prompt.encode("utf-8")) > MAX_PROMPT:
                print("The message is too long. Please shorten it.")
                continue
            turn(channel, editor, prompt)


def run(project, command, environment, tools, files, test=None, *, fleet_home=None):
    editor = Editor(project, tools, files, test, fleet_home=fleet_home)
    proc = None
    try:
        try:
            proc = subprocess.Popen(command, cwd="/", env=environment, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
        except OSError as exc:
            raise ValueError("could not start the CCFleet connection; check the local "
                             "OpenSSH installation") from exc
        session(Channel(proc), editor)
    except (EOFError, BrokenPipeError) as exc:
        raise ValueError("the Gemini editor connection closed; check the private slot's "
                         "editor service and Antigravity sign-in before reconnecting") from exc
    finally:
        editor.workspace.close()
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
