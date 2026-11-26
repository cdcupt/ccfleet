"""Two-CLI native Antigravity terminal over a paired private Mac slot.

The client Mac runs the stock interactive ``agy`` in the user's project and serves its
model endpoint from a loopback adapter. The adapter forwards each turn to the paired
private Mac slot, whose stock headless ``agy`` runs under the owner's Google sign-in and
calls the client's tools through MCP. The client executes every file or shell operation
under its own native review, so the slot never needs a copy of the project.

Protocol (4-byte big-endian length + JSON over the paired SSH transport):

    slot   -> client  {"ready": "ccfleet-native-v1"}
    client -> slot    {"hello": {"v": 1, "root": ..., "tools": {...}}}
    client -> slot    {"turn": T, "prompt": text}
    slot   -> client  {"turn": T, "call": C, "name": tool, "args": {...}}
    client -> slot    {"turn": T, "call": C, "result": {...}}
    client -> slot    {"turn": T, "call": C, "error": text}
    client -> slot    {"turn": T, "cancel": C}
    slot   -> client  {"turn": T, "answer": text, "model": id?} | {"turn": T, "failed": text}
    slot   -> client  {"fatal": text}
    client -> slot    {"bye": true}

One turn holds at most one outstanding call. An explicit local denial, a cancellation or
a lost client releases the held MCP call with an error so the remote conversation stays
synchronized. A dispatched operation with no confirmed outcome is reported as unknown and
is never replayed.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

LIMIT = 4 * 1024 * 1024
CLIENT_TOOLS = ("read_file", "write_file", "edit_file", "run_command")
SERVER = "client"
AGENT = "ccfleet-native"
AUX_TEXT = "CCFleet native session"
ROUTE = re.compile(r"/v1beta/models/[A-Za-z0-9._-]+:streamGenerateContent\?alt=sse")
TURN_WAIT = 3600.0
DRAIN_WAIT = 300.0
TURN_LIMIT = "3600s"
DENIAL_POLL = 1.0
DENIAL_GRACE = 10.0
DENIAL = re.compile(r"denied permission|permission check failed", re.IGNORECASE)
TRANSCRIPT_LIMIT = 8 * 1024 * 1024
REPLAY_LIMIT = 8
REPLAY_AGE = 300.0
MAX_REMOTE_TOOLS = 8
CONVERSATION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
BENIGN_TOOLS = frozenset(
    {"send_message", "list_resources", "read_resource", "manage_task", "finish"}
)


def require(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def diagnostic_path() -> Optional[Path]:
    """Where ``CCFLEET_NATIVE_DEBUG`` sends diagnostics, or None when disabled."""
    value = os.environ.get("CCFLEET_NATIVE_DEBUG", "")
    if not value:
        return None
    if value == "1":
        return Path(tempfile.gettempdir()) / "ccfleet-native-debug.log"
    return Path(value)


def diagnostic(message: str) -> None:
    """Record a diagnostic off the terminal.

    The watchdog counts down in real time, so a diagnostic must never wait on a
    terminal whose reader has stopped draining it.
    """
    target = diagnostic_path()
    if target is None:
        return
    with contextlib.suppress(OSError):
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.time():.3f} {message}\n")


def frame(out: Any, data: dict) -> None:
    raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
    require(0 < len(raw) <= LIMIT, "frame too large")
    out.write(len(raw).to_bytes(4, "big") + raw)
    out.flush()


def receive(inp: Any) -> dict:
    header = inp.read(4)
    if len(header) != 4:
        raise EOFError("slot connection closed")
    size = int.from_bytes(header, "big")
    require(0 < size <= LIMIT, "invalid frame size")
    raw = inp.read(size)
    if len(raw) != size:
        raise EOFError("incomplete slot frame")
    data = json.loads(raw)
    require(isinstance(data, dict), "invalid frame")
    return data


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)


def parts(body: dict) -> list:
    return [p for c in body.get("contents", []) for p in c.get("parts", [])]


def declarations(body: dict) -> dict:
    return {d["name"]: d for t in body.get("tools", []) for d in t.get("functionDeclarations", [])}


def function_responses(body: dict) -> list:
    return [p["functionResponse"] for p in parts(body) if "functionResponse" in p]


def entry_time(entry: dict) -> float:
    """Transcript timestamps are UTC; entries without one never match a denial."""
    try:
        return datetime.strptime(
            str(entry.get("created_at", "")), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def is_continuation(body: dict) -> bool:
    """The request continues a function call instead of carrying a new user prompt."""
    contents = body.get("contents") or []
    parts_ = (contents[-1].get("parts") or []) if contents else []
    return bool(parts_) and all("functionResponse" in p for p in parts_)


def latest_user_text(body: dict) -> str:
    for content in reversed(body.get("contents", [])):
        if content.get("role") != "user":
            continue
        texts = [p["text"] for p in content.get("parts", []) if isinstance(p.get("text"), str)]
        if texts:
            return "\n".join(texts).strip()
    return ""


def select_tools(decls: dict) -> dict:
    """The client tools this session exposes to the remote agent."""
    return {
        name: {
            "description": decl.get("description", ""),
            "parametersJsonSchema": decl["parametersJsonSchema"],
        }
        for name, decl in decls.items()
        if name in CLIENT_TOOLS and isinstance(decl.get("parametersJsonSchema"), dict)
    }


def mcp_schema(schema: dict) -> dict:
    """agy wraps every MCP tool with a toolSummary field; strip it for the remote."""
    cleaned = json.loads(json.dumps(schema))
    properties = cleaned.get("properties")
    if isinstance(properties, dict):
        properties.pop("toolSummary", None)
    if isinstance(cleaned.get("required"), list):
        cleaned["required"] = [k for k in cleaned["required"] if k != "toolSummary"]
    return cleaned


def complete_args(name: str, spec: dict, args: dict) -> dict:
    """Fill the review fields the slot's model cannot know, exactly as the local CLI expects.

    The client's agy requires toolSummary (and a wait hint for commands) while the
    remote only ever sees the same schema without those wrapper fields.
    """
    schema = spec.get("parametersJsonSchema") or {}
    required = schema.get("required") or []
    completed = dict(args)
    if "toolSummary" in required and "toolSummary" not in completed:
        hint = (
            completed.get("CommandLine") or completed.get("Path") or completed.get("Target") or ""
        )
        label = name.replace("_", " ").title()
        completed["toolSummary"] = f"{label}: {hint}".strip()[:90] or label
    if "WaitMsBeforeAsync" in required and "WaitMsBeforeAsync" not in completed:
        completed["WaitMsBeforeAsync"] = 5000
    return completed


def sse(part: dict) -> tuple:
    reply = {
        "candidates": [
            {"index": 0, "finishReason": "STOP", "content": {"role": "model", "parts": [part]}}
        ]
    }
    return "text/event-stream", ("data: " + json.dumps(reply) + "\n\n").encode()


def slot_model_label(model: Any) -> str:
    if not isinstance(model, str) or not MODEL_ID.fullmatch(model):
        return "Paired slot · model not reported"
    if model.startswith("gemini-"):
        parts = model.split("-")
        effort = parts.pop() if parts[-1] in ("low", "medium", "high", "xhigh", "max") else None
        model = " ".join(part.capitalize() for part in parts)
        if effort:
            model += " · " + effort
    return "Last slot reply · " + model


@contextlib.contextmanager
def endpoint(dispatch: Any):
    key = secrets.token_hex(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            try:
                port = self.server.server_port
                require(self.headers.get_all("Host") == [f"127.0.0.1:{port}"], "invalid host")
                require(
                    not self.headers.get("Origin") and not self.headers.get("Transfer-Encoding"),
                    "invalid request",
                )
                keys = self.headers.get_all("X-Goog-Api-Key", [])
                require(len(keys) == 1 and hmac.compare_digest(keys[0], key), "invalid credential")
                sizes = self.headers.get_all("Content-Length", [])
                require(
                    len(sizes) == 1 and sizes[0].isdigit() and 0 < int(sizes[0]) <= LIMIT,
                    "invalid length",
                )
                self.connection.settimeout(10)
                body = json.loads(self.rfile.read(int(sizes[0])))
                require(isinstance(body, dict), "invalid request")
                mime, payload = dispatch(self.path, body)
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (ValueError, OSError, KeyError, TypeError, EOFError, queue.Empty) as exc:
                if diagnostic_path() is not None:
                    print(f"native terminal request failed: {exc}", file=sys.stderr, flush=True)
                with contextlib.suppress(OSError):
                    self.send_error(400, "The native terminal request could not complete.")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", key
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@contextlib.contextmanager
def child(args: Any, **options: Any):
    proc = subprocess.Popen(args, **options)
    try:
        yield proc
    finally:
        grouped = options.get("start_new_session", False)
        if proc.poll() is None:
            if grouped:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                if grouped:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
        proc.wait()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()


def native_profile() -> Path:
    override = os.environ.get("CCFLEET_NATIVE_HOME")
    if override:
        return Path(override)
    return Path.home() / ".config" / "ccfleet" / "native-terminal"


def client_environment(profile: Path, scratch: Path, url: str, key: str) -> dict:
    env = dict(os.environ)
    for name in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_GEMINI_BASE_URL",
        "AGY_ADC_AUTH",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_REGION",
    ):
        env.pop(name, None)
    env.update(
        {
            "HOME": str(profile),
            "TMPDIR": str(scratch),
            "GEMINI_API_KEY": key,
            "GOOGLE_GEMINI_BASE_URL": url,
            "NO_PROXY": "localhost,127.0.0.1",
            "no_proxy": "localhost,127.0.0.1",
            "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
        }
    )
    env.setdefault("LANG", "en_US.UTF-8")
    env.setdefault("TERM", "xterm-256color")
    return env


class Session:
    """Loopback model endpoint for the client's agy; one paired slot session."""

    def __init__(self, relay: Any, project: Path, profile: Path, *, pump: bool = True) -> None:
        self.relay = relay
        self.model_status: Optional[Path] = None
        self.project = project
        self.profile = profile
        self.send_lock = threading.Lock()
        self.main_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.events: queue.Queue = queue.Queue()
        self.closed = threading.Event()
        self.failure = ""
        self.current_turn: Optional[str] = None
        self.turn_open: Optional[str] = None
        self.pending: Optional[dict] = None
        self.denial: Optional[dict] = None
        self.declared: dict = {}
        self.hello_sent = False
        self.replays: dict = {}
        self.replay_order: list = []
        self._started = False
        if pump:
            self.start()

    # --- transport -------------------------------------------------------
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._monitor, daemon=True).start()

    def feed(self, event: dict) -> None:
        self.events.put(event)

    def send(self, message: dict) -> None:
        with self.send_lock:
            frame(self.relay.stdin, message)

    def close(self) -> None:
        with contextlib.suppress(OSError, ValueError, EOFError):
            self.send({"bye": True})
        with contextlib.suppress(OSError):
            self.relay.stdin.close()
        self.closed.set()

    def _pump(self) -> None:
        try:
            while True:
                event = receive(self.relay.stdout)
                if "fatal" in event:
                    with self.state_lock:
                        self.failure = str(event["fatal"])
                    self.closed.set()
                    return
                self.events.put(event)
        except (EOFError, OSError, ValueError) as exc:
            with self.state_lock:
                self.failure = self.failure or str(exc) or "the slot connection closed"
            self.closed.set()

    def _monitor(self) -> None:
        while not self.closed.wait(DENIAL_POLL):
            self._tick()

    def _tick(self) -> None:
        """One watchdog step: release the held call only when the local side stays silent."""
        with self.state_lock:
            pending = dict(self.pending) if self.pending else None
            seen = dict(self.denial) if self.denial else None
        diagnostic(
            f"tick: pending={bool(pending)} seen={bool(seen)} "
            f"grace={time.time() - seen['at'] if seen else 0:.1f}"
        )
        if pending is None:
            with self.state_lock:
                self.denial = None
            return
        if time.time() - pending["at"] > TURN_WAIT:
            self._release(
                pending["call"], "No local operation result arrived; the outcome is unknown."
            )
            return
        if seen is None or seen["call"] != pending["call"]:
            error = self._denial(pending)
            diagnostic(f"watchdog: call {pending['call'][:8]} denial={bool(error)}")
            if error:
                with self.state_lock:
                    self.denial = {"call": pending["call"], "error": error, "at": time.time()}
            return
        if time.time() - seen["at"] >= DENIAL_GRACE:
            # The local CLI's own continuation gets the first chance to carry the
            # denial; this fires only when no continuation ever arrives.
            self._release(pending["call"], seen["error"])

    def _release(self, call_id: str, message: str) -> None:
        with self.state_lock:
            pending = self.pending
            if not pending or pending["call"] != call_id:
                return
            self.pending = None
        diagnostic(f"watchdog: releasing {call_id[:8]} with {message[:40]!r}")
        with contextlib.suppress(OSError, ValueError):
            self.send({"turn": pending["turn"], "call": pending["call"], "error": message})

    def _denial(self, pending: dict) -> Optional[str]:
        brain = self.profile / ".gemini/antigravity-cli/brain"
        newest: Optional[tuple] = None
        for path in brain.glob("*/.system_generated/logs/transcript_full.jsonl"):
            try:
                info = path.stat()
            except OSError:
                continue
            if info.st_size > TRANSCRIPT_LIMIT or info.st_mtime < pending["at"] - 5:
                continue
            if newest is None or info.st_mtime > newest[0]:
                newest = (info.st_mtime, path)
        if newest is None:
            return None
        try:
            entries = [json.loads(line) for line in newest[1].read_bytes().splitlines()]
        except (OSError, ValueError):
            return None
        name, args = pending["name"], pending["args"]
        for before, after in reversed(list(zip(entries, entries[1:]))):
            calls = before.get("tool_calls", [])
            if len(calls) != 1 or calls[0].get("name") != name or calls[0].get("args") != args:
                continue
            error = str(after.get("error", ""))
            if not (
                after.get("status") == "ERROR"
                and after.get("step_index") == before.get("step_index", -2) + 1
                and DENIAL.search(error)
            ):
                continue
            # Freshness belongs to the outcome: a call entry is stamped when its model
            # turn started, which predates the local response by the remote's thinking
            # time, so only a denial recorded after this operation was issued describes it.
            if entry_time(after) < pending["at"] - 5:
                return None
            return error
        return None

    # --- model endpoint --------------------------------------------------
    def dispatch(self, path: str, body: dict) -> tuple:
        require(ROUTE.fullmatch(path), "invalid model route")
        decls = declarations(body)
        if not decls:
            return sse({"text": AUX_TEXT})
        with self.main_lock:
            return self._turn(body, decls)

    def _turn(self, body: dict, decls: dict) -> tuple:
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        cached = self.replays.get(digest)
        if cached is not None and time.time() - cached[0] < REPLAY_AGE:
            return cached[1]
        tools = select_tools(decls)
        require(tools, "the client Antigravity CLI did not declare the expected tools")
        self.declared = decls
        with self.state_lock:
            pending = dict(self.pending) if self.pending else None
        responses = function_responses(body)
        if pending and responses and responses[-1].get("id") == pending["call"]:
            outcome = responses[-1].get("response")
            require(isinstance(outcome, dict), "invalid tool result")
            turn = pending["turn"]
            with self.state_lock:
                self.pending = None
                self.denial = None
            self._send_result(turn, pending["call"], outcome)
            event = self._take(turn)
        elif is_continuation(body):
            # The call was already released (denial, watchdog or interruption), so
            # this result has no turn to continue. Report it instead of replaying
            # the previous prompt as a fresh turn.
            raise ValueError("the local operation result arrived after its turn ended")
        else:
            text = latest_user_text(body)
            require(text, "the client turn carried no user message")
            with self.state_lock:
                stale = dict(self.pending) if self.pending else None
                open_turn = self.turn_open
                self.pending = None
            if stale is not None:
                # The previous operation was interrupted locally without a result;
                # release it so the remote conversation stays synchronized.
                with contextlib.suppress(OSError, ValueError):
                    self.send(
                        {
                            "turn": stale["turn"],
                            "call": stale["call"],
                            "error": "The local operation was interrupted before it finished.",
                        }
                    )
            if open_turn is not None:
                self._drain(open_turn)
            self._hello(tools)
            turn = secrets.token_hex(16)
            with self.state_lock:
                self.current_turn = turn
                self.turn_open = turn
            self.send({"turn": turn, "prompt": text})
            event = self._take(turn)
        attempts = 0
        while True:
            if "failed" in event:
                raise ValueError(str(event["failed"]) or "the slot turn failed")
            if "answer" in event:
                if self.model_status is not None:
                    # Install a complete label before agy renders the corresponding answer.
                    # A display I/O failure must not discard a successful model turn.
                    with contextlib.suppress(OSError):
                        pending = self.model_status.with_suffix(".tmp")
                        pending.write_text(slot_model_label(event.get("model")) + "\n")
                        os.replace(pending, self.model_status)
                response = sse({"text": str(event["answer"])})
                self._remember(digest, response)
                return response
            attempts += 1
            require(attempts <= MAX_REMOTE_TOOLS, "the slot requested too many client tools")
            problem = self._validate_call(event)
            if problem is not None:
                self.send({"turn": turn, "call": event["call"], "error": problem})
                event = self._take(turn)
                continue
            args = complete_args(event["name"], self.declared[event["name"]], event["args"])
            response = sse(
                {
                    "functionCall": {
                        "name": event["name"],
                        "id": event["call"],
                        "args": args,
                    }
                }
            )
            with self.state_lock:
                self.current_turn = turn
                self.pending = {
                    "turn": turn,
                    "call": event["call"],
                    "name": event["name"],
                    "args": args,
                    "at": time.time(),
                }
            self._remember(digest, response)
            return response

    def _hello(self, tools: dict) -> None:
        with self.state_lock:
            if self.hello_sent:
                return
            self.hello_sent = True
        self.send({"hello": {"v": 1, "root": str(self.project), "tools": tools}})

    def _take(self, turn: str, timeout: float = TURN_WAIT) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            require(remaining > 0, "the slot did not answer in time")
            if self.closed.is_set():
                raise ValueError(self.failure or "the slot connection closed")
            try:
                event = self.events.get(timeout=min(1.0, remaining))
            except queue.Empty:
                continue
            if event.get("turn") != turn:
                continue  # stale event from an interrupted turn
            if "answer" in event or "failed" in event:
                with self.state_lock:
                    if self.turn_open == turn:
                        self.turn_open = None
            return event

    def _drain(self, turn: str) -> None:
        """Finish an interrupted turn so the slot accepts the next prompt."""
        while True:
            event = self._take(turn, DRAIN_WAIT)
            if "answer" in event or "failed" in event:
                return
            # The remote asked for another operation after the interruption; the
            # local turn no longer exists, so the operation is refused.
            with contextlib.suppress(OSError, ValueError):
                self.send(
                    {
                        "turn": turn,
                        "call": event["call"],
                        "error": "The local turn was interrupted.",
                    }
                )

    def _validate_call(self, event: dict) -> Optional[str]:
        name = event.get("name")
        args = event.get("args")
        if not isinstance(name, str) or name not in CLIENT_TOOLS:
            return f"unsupported tool: {name}"
        spec = self.declared.get(name)
        if spec is None:
            return f"unsupported tool: {name}"
        if not isinstance(args, dict):
            return f"invalid arguments for {name}"
        # The remote model only sees the schema without agy's MCP toolSummary wrapper.
        required = mcp_schema(spec.get("parametersJsonSchema") or {}).get("required", [])
        missing = [k for k in required if k not in args]
        if missing:
            return f"missing required arguments for {name}: {', '.join(missing)}"
        return None

    def _send_result(self, turn: str, call_id: str, outcome: dict) -> None:
        message = {"turn": turn, "call": call_id, "result": outcome}
        try:
            raw = json.dumps(message, ensure_ascii=False, allow_nan=False).encode()
        except (TypeError, ValueError):
            raw = b""
        if not 0 < len(raw) <= LIMIT:
            self.send({"turn": turn, "call": call_id, "error": "the local result was not returned"})
            return
        with self.send_lock:
            self.relay.stdin.write(len(raw).to_bytes(4, "big") + raw)
            self.relay.stdin.flush()

    def _remember(self, digest: str, response: tuple) -> None:
        self.replays[digest] = (time.time(), response)
        self.replay_order.append(digest)
        while len(self.replay_order) > REPLAY_LIMIT:
            self.replays.pop(self.replay_order.pop(0), None)


def run(binary: Any, project: Any, command: list, profile: Optional[Path] = None,
        *, review: bool = True) -> int:
    """Client role: stock interactive agy in the project, model turns on the slot."""
    binary = Path(binary).resolve(strict=True)
    project = Path(project).resolve(strict=True)
    require(sys.platform == "darwin" and project.is_dir(), "a macOS project directory is required")
    profile = Path(profile) if profile else native_profile()
    profile.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(profile, 0o700)
    settings = profile / ".gemini/antigravity-cli/settings.json"
    expected = {"modelProvider": "gemini", "agentMode": "", "toolPermission": "request-review"}
    try:
        current = json.loads(settings.read_text())
    except (OSError, ValueError):
        current = None
    merged = dict(current) if isinstance(current, dict) else {}
    if any(merged.get(k) != v for k, v in expected.items()):
        write_json(settings, {**merged, **expected})
    with child(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd="/",
        start_new_session=True,
    ) as relay:
        ready = receive(relay.stdout)
        require(
            ready.get("ready") == "ccfleet-native-v1",
            "the paired slot does not offer the native terminal",
        )
        session = Session(relay, project, profile)
        lost = False
        try:
            with endpoint(session.dispatch) as (url, key), tempfile.TemporaryDirectory(
                prefix="ccfleet-native-client-", dir="/private/tmp"
            ) as scratch:
                env = client_environment(profile, Path(scratch), url, key)
                session.model_status = Path(scratch) / "slot-model.txt"
                session.model_status.write_text("Paired slot · awaiting first reply\n")
                # agy's startup header identifies its local API route. The slot only
                # reports the model that answered once a turn has completed.
                alias = "ccfleet-paired-slot"
                ui_settings = {
                    "customModelsConfig": {
                        "customModels": {
                            alias: {"modelName": "gemini-api://" + alias,
                                    "label": "CCFleet paired slot"},
                        },
                    },
                    "statusLine": {
                        "type": "command", "stack_with_default": False,
                        "command": "/bin/cat " + shlex.quote(str(session.model_status)),
                    },
                }
                ui_command = [str(binary), "--settings", json.dumps(ui_settings), "--model", alias]
                if review:
                    notice = "Local tool approval prompts enabled."
                else:
                    ui_command.append("--dangerously-skip-permissions")
                    notice = ("Full access on this Mac. File edits and shell commands can run "
                              "without approval prompts. Use ccfleet gemini --review for prompts.")
                print("CCFleet native terminal: model turns run on your paired slot. " + notice,
                      flush=True)
                with child(ui_command, cwd=str(project), env=env) as ui:
                    while ui.poll() is None:
                        if session.closed.is_set():
                            print(
                                "\nThe paired slot connection was lost: "
                                f"{session.failure or 'no reason was reported'}",
                                file=sys.stderr,
                            )
                            lost = True
                            break
                        time.sleep(0.2)
        finally:
            session.close()
    return 2 if lost else 0


def bridge_environment(home: Path, session: Path) -> dict:
    scratch = session / "tmp"
    scratch.mkdir(mode=0o700)
    return {
        "HOME": str(home),
        "TMPDIR": str(scratch),
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "TERM": "dumb",
        "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
    }


def session_settings(tools: dict) -> dict:
    return {
        "agentMode": "",
        "toolPermission": "request-review",
        "permissions": {"allow": [f"mcp({SERVER}/{name})" for name in tools]},
    }


def remote_tool_allowed(name: str) -> bool:
    """Only the bridged client tools and harmless session tools may be called."""
    return name in BENIGN_TOOLS or name.startswith(f"mcp_{SERVER}_")


def transcript_path(conversation: str) -> Optional[Path]:
    if not CONVERSATION.fullmatch(conversation or ""):
        return None
    home = Path.home() / ".gemini" / "antigravity-cli" / "brain" / conversation
    return home / ".system_generated/logs/transcript_full.jsonl"


def read_transcript(path: Optional[Path]) -> list:
    if path is None:
        return []
    try:
        if not 0 < path.stat().st_size <= TRANSCRIPT_LIMIT:
            return []
        return [json.loads(line) for line in path.read_bytes().splitlines()]
    except (OSError, ValueError):
        return []


def turn_tool_names(entries: list) -> set:
    names = set()
    for entry in entries:
        for call in entry.get("tool_calls") or []:
            name = str(call.get("name") or "")
            if name:
                names.add(name)
    return names


def turn_reply(entries: list, submitted: int) -> Optional[dict]:
    # A fresh slot conversation receives one USER_INPUT per submitted user turn.
    # A delayed or incomplete transcript cannot attest to the current reply.
    if not submitted or sum(e.get("type") == "USER_INPUT" for e in entries) != submitted:
        return None
    for entry in reversed(entries):
        if entry.get("type") == "USER_INPUT":
            break
        if entry.get("type") == "PLANNER_RESPONSE" and entry.get("status") == "DONE":
            content = entry.get("content")
            if isinstance(content, str) and content.strip():
                return entry
    return None


def bridge_rules(root: str) -> str:
    return (
        f"The user's project is on their connecting computer at {root}. Every file read, "
        "write, edit and shell command must use the client MCP tools (they are the only file "
        "and command tools available to you) with absolute paths under that project root. "
        "The current directory on this host is an empty session directory and holds no "
        "project files. The user reviews every operation on their computer and may reject or "
        "interrupt it; if a tool reports a denial or an interruption, explain it and stop "
        "rather than retrying the same operation."
    )


def mcp_reply(message: dict, tools: dict, call: Any) -> Optional[dict]:
    """JSON-RPC reply for one MCP request; ``call(name, args)`` returns an outcome."""
    if "id" not in message:
        return None
    method = message.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": message["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER, "version": "1"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": name,
                    "inputSchema": mcp_schema(spec["parametersJsonSchema"]),
                    "description": spec.get("description", ""),
                }
                for name, spec in tools.items()
            ]
        }
    elif method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in tools:
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32602, "message": "unsupported tool"},
            }
        arguments = {k: v for k, v in arguments.items() if k != "toolSummary"}
        try:
            outcome = call(name, arguments)
        except (OSError, ValueError, EOFError) as exc:
            outcome = {"error": str(exc) or "the client operation failed"}
        result = {
            "content": [{"type": "text", "text": json.dumps(outcome)}],
            "isError": "error" in outcome,
        }
    elif method == "ping":
        result = {}
    else:
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "error": {"code": -32601, "message": "unsupported"},
        }
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


class Bridge:
    """Slot role: one stock headless agy plus the client's MCP tools."""

    def __init__(self, inp: Any, out: Any, binary: Any, authorized: Any) -> None:
        self.inp, self.out = inp, out
        self.binary = Path(binary)
        self.authorized = authorized
        self.send_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.agy_lock = threading.Lock()
        self.turn: Optional[str] = None
        self.submitted = 0
        self.call: Optional[str] = None
        self.result: Optional[dict] = None
        self.result_ready = threading.Event()
        self.tools: dict = {}
        self.root = ""
        self.conversation = ""
        self.proc: Optional[subprocess.Popen] = None
        self.failure = ""
        self._stopped = threading.Event()

    # --- transport -------------------------------------------------------
    def send(self, message: dict) -> None:
        with self.send_lock:
            frame(self.out, message)

    def run(self) -> int:
        require(self.authorized(), "workspace grant required")
        self.send({"ready": "ccfleet-native-v1"})
        hello = receive(self.inp)
        self.root, self.tools = self.validate_hello(hello)
        with tempfile.TemporaryDirectory(
            prefix="ccfleet-native-slot-", dir="/private/tmp"
        ) as raw_session:
            session = Path(raw_session)
            workspace = session / "empty"
            workspace.mkdir()
            self.configure(session, workspace)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(session / "mcp.sock"))
                listener.listen(1)
                argv = [
                    str(self.binary),
                    "--agent",
                    AGENT,
                    "--settings",
                    json.dumps(session_settings(self.tools), separators=(",", ":")),
                    "--disable-slash-commands",
                    "--input-format",
                    "stream-json",
                    "--output-format",
                    "stream-json",
                    "--print-timeout",
                    TURN_LIMIT,
                ]
                self.proc = subprocess.Popen(
                    argv,
                    cwd=str(session / "empty"),
                    env=bridge_environment(Path.home(), session),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                for target in (
                    lambda: self._agy(),
                    lambda: self._mcp(listener),
                    lambda: self._watch(),
                ):
                    threading.Thread(target=target, daemon=True).start()
                try:
                    self._transport()
                except (ValueError, OSError, EOFError) as exc:
                    self.fail(str(exc) or "the native terminal bridge stopped")
                finally:
                    self._stop()
        return 0

    def validate_hello(self, message: dict) -> tuple:
        hello = message.get("hello")
        require(isinstance(hello, dict), "invalid native handshake")
        require(hello.get("v") == 1, "unsupported native handshake")
        root = hello.get("root")
        require(
            isinstance(root, str) and root.startswith("/") and ".." not in root.split("/"),
            "invalid project root",
        )
        tools = hello.get("tools")
        require(isinstance(tools, dict) and tools, "missing client tools")
        for name, spec in tools.items():
            require(isinstance(name, str) and name in CLIENT_TOOLS, "unexpected client tool")
            require(
                isinstance(spec, dict) and isinstance(spec.get("parametersJsonSchema"), dict),
                "invalid client tool schema",
            )
        return root, tools

    def configure(self, session: Path, workspace: Path) -> None:
        # agy reads project-scoped MCP servers and agents from its working directory.
        folder = workspace / ".agents"
        folder.mkdir(mode=0o700)
        server = {
            "command": sys.executable,
            "args": ["-I", "-B", str(Path(__file__).resolve()), "mcp", str(session / "mcp.sock")],
            "tools": {name: {"eager": True} for name in self.tools},
            "timeoutSeconds": int(TURN_WAIT),
        }
        (folder / "mcp_config.json").write_text(json.dumps({"mcpServers": {SERVER: server}}))
        agents = folder / "agents"
        agents.mkdir()
        (agents / f"{AGENT}.md").write_text(
            "---\n"
            f"name: {AGENT}\n"
            "description: CCFleet native terminal bridge\n"
            "mainAgent: true\n"
            "subagent: false\n"
            "tools: [send_message]\n"
            "inheritMcp: true\n"
            "inheritCustomizations: false\n"
            "---\n" + bridge_rules(self.root) + "\n"
        )
        (workspace / "GEMINI.md").write_text(bridge_rules(self.root) + "\n")

    def _transport(self) -> None:
        while True:
            message = receive(self.inp)
            if message.get("bye"):
                return
            turn = message.get("turn")
            require(isinstance(turn, str), "invalid turn")
            if "prompt" in message:
                with self.state_lock:
                    require(self.turn is None, "a turn is already running")
                    self.turn = turn
                    self.submitted += 1
                    self.call = None
                require(self.authorized(), "workspace grant expired")
                with self.agy_lock:
                    require(self.proc is not None and self.proc.poll() is None, "the agent stopped")
                    line = json.dumps({"event": "user", "message": {"content": message["prompt"]}})
                    self.proc.stdin.write(line.encode() + b"\n")
                    self.proc.stdin.flush()
            elif "call" in message and ("result" in message or "error" in message):
                outcome = message.get("result")
                if "error" in message:
                    outcome = {"error": message["error"]}
                self._deliver(turn, message["call"], outcome)
            elif "call" in message and "cancel" in message:
                self._deliver(
                    turn,
                    message["cancel"],
                    {"error": "The local operation was interrupted before it finished."},
                )
            else:
                raise ValueError("unsupported native terminal message")

    def _deliver(self, turn: str, call_id: str, outcome: Any) -> None:
        require(isinstance(outcome, dict), "invalid tool outcome")
        with self.state_lock:
            if self.turn != turn or self.call != call_id or self.result is not None:
                return
            self.result = outcome
            self.result_ready.set()

    def call_client(self, name: str, args: dict) -> dict:
        with self.state_lock:
            turn = self.turn
            require(turn is not None, "the client has no active turn")
            require(self.call is None, "a client operation is already outstanding")
            call = secrets.token_hex(16)
            self.call = call
            self.result = None
            self.result_ready.clear()
        require(self.authorized(), "workspace grant expired")
        self.send({"turn": turn, "call": call, "name": name, "args": args})
        self.result_ready.wait(TURN_WAIT)
        with self.state_lock:
            outcome = self.result
            self.call = None
            self.result = None
            self.result_ready.clear()
        if not isinstance(outcome, dict):
            return {"error": "No local result arrived; the operation outcome is unknown."}
        return outcome

    def _mcp(self, listener: socket.socket) -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn, conn.makefile("rb") as reader, conn.makefile("wb") as writer:
            try:
                for raw in reader:
                    if len(raw) > LIMIT:
                        return
                    try:
                        message = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(message, dict):
                        continue
                    reply = mcp_reply(message, self.tools, self.call_client)
                    if reply is not None:
                        writer.write(json.dumps(reply).encode() + b"\n")
                        writer.flush()
            except (OSError, ValueError):
                return

    def _agy(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("event") == "init":
                    conversation = event.get("conversation_id")
                    if isinstance(conversation, str) and CONVERSATION.fullmatch(conversation):
                        self.conversation = conversation
                elif event.get("event") == "result":
                    self._finish(event.get("result") or {})
        except OSError:
            return

    def _finish(self, result: dict) -> None:
        with self.state_lock:
            turn = self.turn
            submitted = self.submitted
            self.turn = None
            self.call = None
            self.result = None
            self.result_ready.set()
        if turn is None:
            return
        entries = read_transcript(transcript_path(self.conversation))
        outside = sorted({n for n in turn_tool_names(entries) if not remote_tool_allowed(n)})
        with contextlib.suppress(OSError, ValueError):
            if outside:
                # A slot-side tool ran outside the client's review; stop the session.
                self.send(
                    {
                        "turn": turn,
                        "fatal": "the slot agent used local tools: " + ", ".join(outside),
                    }
                )
                self._stop()
                return
            if result.get("status") == "SUCCESS":
                reply = turn_reply(entries, submitted)
                answer = reply["content"].strip() if reply else result.get("response")
                message = {"turn": turn, "answer": answer if isinstance(answer, str) else ""}
                model = reply.get("model") if reply else None
                if isinstance(model, str) and MODEL_ID.fullmatch(model):
                    message["model"] = model
                self.send(message)
            else:
                reason = result.get("error") or result.get("status") or "failed"
                self.send({"turn": turn, "failed": str(reason)})

    def _watch(self) -> None:
        while not self._stopped.wait(2):
            try:
                allowed = self.authorized()
            except (OSError, ValueError):
                allowed = False
            if not allowed:
                self.fail("workspace grant expired")
                return

    def fail(self, message: str) -> None:
        self.failure = message
        with contextlib.suppress(OSError, ValueError):
            self.send({"fatal": message})
        self._stop()

    def _stop(self) -> None:
        self._stopped.set()
        proc = self.proc
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()


def bridge(inp: Any, out: Any, binary: Any, authorized: Any) -> int:
    return Bridge(inp, out, binary, authorized).run()


def mcp(path: str) -> int:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.connect(path)
        with conn.makefile("rb") as reader, conn.makefile("wb") as writer:
            source = sys.stdin.buffer
            while True:
                line = source.readline()
                if not line:
                    return 0
                if len(line) > LIMIT:
                    return 2
                try:
                    message = json.loads(line)
                except ValueError:
                    return 2
                writer.write(line)
                writer.flush()
                if "id" in message and "method" in message:
                    reply = reader.readline(LIMIT + 1)
                    if not reply or len(reply) > LIMIT:
                        return 2
                    sys.stdout.buffer.write(reply)
                    sys.stdout.buffer.flush()


def main(argv: Optional[list] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "mcp":
        return mcp(args[1])
    if len(args) == 2 and args[0] == "bridge-stdin":
        binary = Path(args[1]).resolve(strict=True)
        return bridge(sys.stdin.buffer, sys.stdout.buffer, binary, lambda: True)
    if args and args[0] == "loopback" and len(args) <= 2:
        project = Path(args[1] if len(args) == 2 else ".")
        binary = shutil.which("agy") or str(Path.home() / ".local/bin/agy")
        command = [
            sys.executable,
            "-I",
            "-B",
            str(Path(__file__).resolve()),
            "bridge-stdin",
            str(binary),
        ]
        run(Path(binary), project, command)
        return 0
    raise ValueError("usage: gemini_native_terminal.py loopback [project-directory]")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, EOFError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
