"""Interactive two-CLI acceptance probe. Both Gemini model endpoints are deterministic."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LIMIT = 4 * 1024 * 1024
TIMEOUT = 180
TOOLS = ("read_file", "edit_file", "run_command")
FINAL = "Native tool verification completed."
PROMPT = (
    "Read the proof file, accept the first edit, run the Git command, then reject the second edit."
)
ROUTE = re.compile(r"/v1beta/models/[A-Za-z0-9._-]+:streamGenerateContent\?alt=sse")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def plan(root):
    return [
        ("read_file", {"Path": str(root / "proof.txt")}),
        (
            "edit_file",
            {"Path": str(root / "approved.txt"), "Target": "before", "Replacement": "after"},
        ),
        (
            "run_command",
            {
                "CommandLine": "git rev-parse --show-toplevel",
                "toolSummary": "Git repository root",
                "WaitMsBeforeAsync": 5000,
            },
        ),
        (
            "edit_file",
            {"Path": str(root / "denied.txt"), "Target": "unchanged", "Replacement": "unexpected"},
        ),
    ]


def frame(out, data):
    raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
    require(0 < len(raw) <= LIMIT, "frame too large")
    out.write(len(raw).to_bytes(4, "big") + raw)
    out.flush()


def receive(inp):
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


def write_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def parts(body):
    return [p for c in body.get("contents", []) for p in c.get("parts", [])]


def declarations(body):
    return {d["name"]: d for t in body.get("tools", []) for d in t.get("functionDeclarations", [])}


def sse(part):
    reply = {
        "candidates": [
            {"index": 0, "finishReason": "STOP", "content": {"role": "model", "parts": [part]}}
        ]
    }
    return "text/event-stream", ("data: " + json.dumps(reply) + "\n\n").encode()


@contextlib.contextmanager
def endpoint(dispatch):
    key = secrets.token_hex(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
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
            except (ValueError, OSError, KeyError, TypeError, EOFError, queue.Empty):
                with contextlib.suppress(OSError):
                    self.send_error(400, "The native verification request could not complete.")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", key
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def mcp(url, key):
    for line in sys.stdin.buffer:
        require(len(line) <= LIMIT, "invalid MCP request size")
        msg = json.loads(line)
        if "id" not in msg:
            continue
        method = msg.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": msg["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "client", "version": "1"},
            }
        elif method in ("tools/list", "tools/call"):
            req = urllib.request.Request(
                url + "/" + method,
                json.dumps(msg.get("params", {})).encode(),
                {"X-Goog-Api-Key": key, "Content-Type": "application/json"},
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=TIMEOUT) as reply:
                result = json.load(reply)
        elif method == "ping":
            result = {}
        else:
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "error": {"code": -32601, "message": "unsupported"},
                    }
                ),
                flush=True,
            )
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)


@contextlib.contextmanager
def child(args, **options):
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
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if grouped:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
        proc.wait()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


def setup(root, url, key, *, backend):
    profile = root / "profile"
    tmp = root / "tmp"
    tmp.mkdir()
    cfg = profile / ".gemini/antigravity-cli/settings.json"
    settings = {"modelProvider": "gemini", "agentMode": "", "toolPermission": "request-review"}
    if backend:
        settings["permissions"] = {"allow": [f"mcp(client/{name})" for name in TOOLS]}
    write_json(cfg, settings)
    return {
        "HOME": str(profile),
        "TMPDIR": str(tmp) + "/",
        "PATH": os.defpath,
        "TERM": os.environ.get("TERM", "xterm-256color"),
        "LANG": "en_US.UTF-8",
        "SHELL": "/bin/sh",
        "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
        "GEMINI_API_KEY": key,
        "GOOGLE_GEMINI_BASE_URL": url,
        "NO_PROXY": "localhost,127.0.0.1",
    }


def sandbox(binary, lab, allowed_repo=None, denied_repo=None):
    # Both processes can read their own profile. Only the client can access the fixture.
    policy = [
        "(version 1)(allow default)(deny network*)",
        '(allow network-bind network-inbound (local ip "localhost:*"))',
        '(allow network-outbound (remote ip "localhost:*"))',
        '(deny mach-lookup (global-name "com.apple.securityd") (global-name "com.apple.secd"))',
        '(deny file-read* (subpath (param "REALHOME")))(deny file-write*)',
        '(allow file-read* (literal (param "BINARY")) (literal (param "HELPER")))',
        '(allow file-read* file-write* (subpath (param "LAB")))',
        '(allow file-write* (literal "/dev/null") (literal "/dev/tty") (literal "/dev/ptmx") '
        '(regex #"^/dev/ttys[0-9]+$"))',
    ]
    argv = [
        "/usr/bin/sandbox-exec",
        "-D",
        f"REALHOME={Path.home()}",
        "-D",
        f"BINARY={binary}",
        "-D",
        f"HELPER={Path(__file__).resolve()}",
        "-D",
        f"LAB={lab}",
    ]
    if allowed_repo:
        argv += ["-D", f"FIXTURE={allowed_repo}"]
        policy.append('(allow file-read* file-write* (subpath (param "FIXTURE")))')
    if denied_repo is not None:
        argv += ["-D", f"CLIENTREPO={denied_repo}"]
        policy.append('(deny file-read* file-write* (subpath (param "CLIENTREPO")))')
    return argv + ["-p", "\n".join(policy), str(binary)]


class Remote:
    def __init__(self, hello, inp, out, authorized):
        require(
            hello.get("version") == 1 and isinstance(hello.get("root"), str),
            "invalid native handshake",
        )
        root = Path(hello["root"])
        require(
            root.is_absolute()
            and root.name.startswith(".ccfleet-native-")
            and ".." not in root.parts,
            "invalid fixture path",
        )
        self.steps = plan(root)
        self.spec = hello["tools"]
        require(
            isinstance(self.spec, dict) and set(self.spec) == set(TOOLS),
            "missing native tool declarations",
        )
        self.marker_hash = hello["marker_hash"]
        require(re.fullmatch("[0-9a-f]{64}", self.marker_hash), "invalid fixture digest")
        self.root, self.inp, self.out, self.authorized = root, inp, out, authorized
        self.finished = 0
        self.confirmed = 0
        self.lock = threading.Lock()

    def verify(self, n, outcome):
        value = outcome.get("output", "")
        if n == 0:
            match = re.search(r"native-marker-[0-9a-f]{48}", value)
            require(
                match and hashlib.sha256(match[0].encode()).hexdigest() == self.marker_hash,
                "read proof failed",
            )
        elif n == 1:
            require("Made 1 replacement" in value, "edit was not accepted")
        elif n == 2:
            require(
                "Command exited with code 0" in value and str(self.root) in value.splitlines(),
                "unexpected Git root",
            )
        else:
            require(
                f"user denied permission for write_file({self.root / 'denied.txt'})"
                in outcome.get("error", ""),
                "edit denial missing",
            )

    def dispatch(self, path, body):
        require(self.authorized(), "workspace grant expired")
        if path == "/tools/list":
            tools = []
            for name in TOOLS:
                schema = json.loads(json.dumps(self.spec[name]["parametersJsonSchema"]))
                # agy adds toolSummary when wrapping every MCP tool, including shell.
                schema["properties"].pop("toolSummary", None)
                schema["required"] = [k for k in schema.get("required", []) if k != "toolSummary"]
                tools.append(
                    {
                        "name": name,
                        "inputSchema": schema,
                        "description": self.spec[name]["description"],
                    }
                )
            result = {"tools": tools}
        elif path == "/tools/call":
            with self.lock:
                n = self.finished
                require(n < len(self.steps), "extra MCP call")
                name, args = self.steps[n]
                received = body.get("arguments", {}).copy()
                received.pop("toolSummary", None)
                expected = {k: v for k, v in args.items() if k != "toolSummary"}
                require(
                    body.get("name") == name and received == expected, "unexpected remote tool call"
                )
                ident = secrets.token_hex(16)
                frame(self.out, {"call": ident, "name": name, "args": args})
                reply = receive(self.inp)
                require(
                    reply.get("call") == ident and self.authorized(),
                    "tool transaction no longer active",
                )
                outcome = reply["result"]
                self.verify(n, outcome)
                self.finished += 1
                result = {
                    "content": [{"type": "text", "text": json.dumps(outcome)}],
                    "isError": "error" in outcome,
                }
        else:
            require(ROUTE.fullmatch(path), "invalid model route")
            ds = declarations(body)
            if not ds:
                return sse({"text": "Native verification"})
            require(not set(TOOLS) & set(ds), "remote built-in file or terminal tool exposed")
            results = [p["functionResponse"] for p in parts(body) if "functionResponse" in p]
            n = self.finished
            require(len(results) == n, "remote model did not receive the outstanding tool result")
            if n:
                last = results[-1]
                expected = "mcp_client_" + self.steps[n - 1][0]
                require(last["name"] == expected, "remote result belongs to a different tool")
                rendered = json.dumps(last["response"])
                if n == 1:
                    match = re.search(r"native-marker-[0-9a-f]{48}", rendered)
                    require(
                        match and hashlib.sha256(match[0].encode()).hexdigest() == self.marker_hash,
                        "backend lost marker",
                    )
                if n == len(self.steps):
                    require("user denied permission" in rendered, "backend lost denial")
                self.confirmed = n
            if n == len(self.steps):
                return sse({"text": FINAL})
            name, args = self.steps[n]
            remote_name = "mcp_client_" + name
            require(remote_name in ds, "MCP bridge tool missing")
            return sse(
                {
                    "functionCall": {
                        "name": remote_name,
                        "args": {"toolSummary": "Native verification", **args},
                    }
                }
            )
        return "application/json", json.dumps(result).encode()


def slot(inp, out, binary, authorized):
    require(authorized(), "workspace grant required")
    frame(out, {"ready": "ccfleet-native-check-v1"})
    remote = Remote(receive(inp), inp, out, authorized)
    with (
        tempfile.TemporaryDirectory(prefix="ccfleet-native-slot-", dir="/private/tmp") as temp,
        endpoint(remote.dispatch) as (url, key),
    ):
        lab = Path(temp)
        env = setup(lab, url, key, backend=True)
        cfg = Path(env["HOME"]) / ".gemini/config"
        helper = lab / "probe.py"
        shutil.copyfile(__file__, helper)
        write_json(
            cfg / "mcp_config.json",
            {
                "mcpServers": {
                    "client": {
                        "command": "/usr/bin/python3",
                        "args": ["-I", str(helper), "mcp", url],
                        "env": {"NATIVE_CHECK_KEY": key},
                        "tools": {n: {"eager": True} for n in TOOLS},
                        "timeoutSeconds": TIMEOUT,
                    }
                }
            },
        )
        agents = cfg / "agents"
        agents.mkdir()
        (agents / "fixture-bridge.md").write_text(
            "---\nname: fixture-bridge\ndescription: Native verification\n"
            "mainAgent: true\nsubagent: false\ntools: [send_message]\ninheritMcp: true\n"
            "inheritCustomizations: false\n---\nUse the client MCP tools.\n"
        )
        cwd = lab / "empty"
        cwd.mkdir()
        argv = sandbox(binary, lab, denied_repo=remote.root) + [
            "--agent",
            "fixture-bridge",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--print-timeout",
            f"{TIMEOUT}s",
        ]
        with child(
            argv,
            cwd=cwd,
            env=env,
            start_new_session=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as proc:
            stopped = threading.Event()

            def watch_grant():
                deadline = time.monotonic() + TIMEOUT
                while not stopped.wait(1):
                    try:
                        valid = time.monotonic() < deadline and authorized()
                    except (OSError, ValueError, KeyError):
                        valid = False
                    if not valid:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(proc.pid, signal.SIGTERM)
                        return

            watchdog = threading.Thread(target=watch_grant, daemon=True)
            watchdog.start()
            try:
                proc.stdin.write(
                    (
                        json.dumps(
                            {
                                "event": "user",
                                "message": {"content": "Perform the native verification."},
                            }
                        )
                        + "\n"
                    ).encode()
                )
                proc.stdin.close()
                cid = None
                for raw in proc.stdout:
                    event = json.loads(raw)
                    if event.get("event") == "init":
                        cid = event.get("conversation_id")
                    if event.get("event") == "result":
                        r = event["result"]
                        require(
                            cid
                            and r["conversation_id"] == cid
                            and r["status"] == "SUCCESS"
                            and r["response"].strip() == FINAL
                            and remote.confirmed == 4
                            and authorized(),
                            "backend verification incomplete",
                        )
                        frame(out, {"verified": 4})
                        return 0
            finally:
                stopped.set()
                watchdog.join(2)
    raise ValueError("slot stopped without a verified result")


class Client:
    def __init__(self, project, marker, transport):
        self.project, self.marker, self.transport = project, marker, transport
        self.steps = plan(project)
        self.index = 0
        self.pending = None
        self.sent = set()
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.passed = False
        self.error = ""

    def fail(self, reason):
        self.error = reason
        self.done.set()

    def next_response(self):
        msg = receive(self.transport.stdout)
        if msg == {"verified": 4}:
            require(self.index == 4, "premature completion")
            require(
                (self.project / "approved.txt").read_text() == "after\n", "approved file mismatch"
            )
            require(
                (self.project / "denied.txt").read_text() == "unchanged\n", "rejected file changed"
            )
            self.passed = True
            self.done.set()
            return sse({"text": FINAL})
        require(self.index < len(self.steps), "extra operation")
        name, args = self.steps[self.index]
        ident = msg.get("call")
        require(
            isinstance(ident, str)
            and re.fullmatch("[a-f0-9]{32}", ident)
            and ident not in self.sent,
            "invalid operation ID",
        )
        require(
            msg == {"call": ident, "name": name, "args": args},
            "slot requested an unexpected operation",
        )
        self.sent.add(ident)
        self.pending = ident
        return sse({"functionCall": {"name": name, "id": ident, "args": args}})

    def finish(self, outcome):
        ident = self.pending
        require(ident is not None and isinstance(outcome, dict), "missing pending tool")
        self.pending = None
        self.index += 1
        frame(self.transport.stdin, {"call": ident, "result": outcome})
        return self.next_response()

    def dispatch(self, path, body):
        require(ROUTE.fullmatch(path), "invalid model route")
        ds = declarations(body)
        if not ds:
            return sse({"text": "CCFleet native check"})
        with self.lock:
            try:
                if self.passed:
                    return sse({"text": FINAL})
                require(not self.done.is_set(), "session is closed")
                results = [p["functionResponse"] for p in parts(body) if "functionResponse" in p]
                if self.pending is None:
                    require(
                        self.index == 0 and not self.sent and not results,
                        "unexpected initial request",
                    )
                    require(
                        all(n in ds and "parametersJsonSchema" in ds[n] for n in TOOLS),
                        "CLI tool schemas changed",
                    )
                    digest = hashlib.sha256(self.marker.encode()).hexdigest()
                    frame(
                        self.transport.stdin,
                        {
                            "version": 1,
                            "root": str(self.project),
                            "marker_hash": digest,
                            "tools": {n: ds[n] for n in TOOLS},
                        },
                    )
                    return self.next_response()
                require(
                    results
                    and (results[-1].get("id"), results[-1].get("name"))
                    == (self.pending, self.steps[self.index][0]),
                    "unexpected or repeated request; operation outcome is uncertain",
                )
                return self.finish(results[-1]["response"])
            except (ValueError, KeyError, TypeError, OSError, EOFError) as e:
                self.fail(str(e))
                raise

    def observe_denial(self, profile):
        # Every run has a fresh CLI profile and exactly one outstanding operation. Only the
        # harness's structured error immediately following our exact proposed edit is evidence.
        if self.index != 3 or self.pending is None:
            return
        paths = list(
            (profile / ".gemini/antigravity-cli/brain").glob(
                "*/.system_generated/logs/transcript_full.jsonl"
            )
        )
        if len(paths) != 1 or paths[0].stat().st_size > LIMIT:
            return
        try:
            entries = [json.loads(line) for line in paths[0].read_bytes().splitlines()]
        except (OSError, ValueError):
            return
        name, args = self.steps[3]
        for before, after in zip(entries, entries[1:]):
            calls = before.get("tool_calls", [])
            if len(calls) != 1 or calls[0].get("name") != name or calls[0].get("args") != args:
                continue
            error = after.get("error", "")
            if (
                after.get("status") == "ERROR"
                and after.get("step_index") == before.get("step_index", -2) + 1
                and f"user denied permission for write_file({args['Path']})" in error
            ):
                with self.lock:
                    if self.index == 3 and self.pending is not None:
                        self.finish({"error": error})
                return


def run(binary, project, command):
    binary = Path(binary).resolve(strict=True)
    project = Path(project).resolve(strict=True)
    require(sys.platform == "darwin" and project.is_dir(), "a macOS project directory is required")
    with (
        tempfile.TemporaryDirectory(prefix="ccfleet-native-client-", dir="/private/tmp") as tmp,
        tempfile.TemporaryDirectory(prefix=".ccfleet-native-", dir=project) as fixture,
        child(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd="/",
            start_new_session=True,
        ) as relay,
    ):
        lab, repo = Path(tmp), Path(fixture)
        timeout = threading.Timer(TIMEOUT, relay.terminate)
        timeout.daemon = True
        timeout.start()
        try:
            require(
                receive(relay.stdout) == {"ready": "ccfleet-native-check-v1"},
                "native slot check unavailable",
            )
            marker = "native-marker-" + secrets.token_hex(24)
            (repo / "proof.txt").write_text(marker + "\n")
            (repo / "approved.txt").write_text("before\n")
            (repo / "denied.txt").write_text("unchanged\n")
            # A caller may already have GIT_DIR or a work tree set. Initialize only the fixture.
            subprocess.run(
                ["/usr/bin/git", "init", "-q", "--template=", str(repo)],
                env={
                    "PATH": os.defpath,
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_NOSYSTEM": "1",
                },
                check=True,
            )
            client = Client(repo, marker, relay)
            with endpoint(client.dispatch) as (url, key):
                env = setup(lab, url, key, backend=False)
                argv = sandbox(binary, lab, repo) + [
                    "--log-file",
                    str(lab / "client.log"),
                    "-i",
                    PROMPT,
                ]
                print(
                    "Allow the read, accept approved.txt, allow Git, and reject denied.txt.",
                    flush=True,
                )
                with child(argv, cwd=repo, env=env) as ui:
                    deadline = time.monotonic() + TIMEOUT
                    while not client.done.wait(0.15):
                        require(
                            time.monotonic() < deadline and relay.poll() is None,
                            "slot closed or timed out; no operation was retried",
                        )
                        require(ui.poll() is None, "interactive verification interrupted")
                        client.observe_denial(Path(env["HOME"]))
                    require(client.passed, client.error or "native check incomplete")
        finally:
            timeout.cancel()
    print(
        "PASS: private marker, one approved edit, local Git root, and same-session rejection.",
        flush=True,
    )


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "mcp":
        return mcp(sys.argv[2], os.environ["NATIVE_CHECK_KEY"])
    if len(sys.argv) == 3 and sys.argv[1] == "fixture-slot":
        return slot(
            sys.stdin.buffer,
            sys.stdout.buffer,
            Path(sys.argv[2]).resolve(strict=True),
            lambda: True,
        )
    if len(sys.argv) in (2, 3) and sys.argv[1] == "loopback":
        binary = shutil.which("agy") or str(Path.home() / ".local/bin/agy")
        run(
            binary,
            Path(sys.argv[2] if len(sys.argv) == 3 else "."),
            [sys.executable, "-I", __file__, "fixture-slot", binary],
        )
        return 0
    raise ValueError("usage: python3 probe.py loopback [project-directory]")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, EOFError, subprocess.SubprocessError) as exc:
        sys.exit(str(exc))
