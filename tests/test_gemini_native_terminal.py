import hashlib
import http.client
import io
import json
import runpy
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleet_agent import gemini_native_terminal as terminal

ROOT = Path(__file__).resolve().parents[1]

SCHEMA = {
    "properties": {"Path": {"type": "string"}, "toolSummary": {"type": "string"}},
    "required": ["Path", "toolSummary"],
}
TOOLS = [{"functionDeclarations": [{"name": "read_file", "parametersJsonSchema": SCHEMA}]}]


def packet(value):
    stream = io.BytesIO()
    terminal.frame(stream, value)
    stream.seek(0)
    return stream


def relay():
    return SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO())


def frames(stream):
    raw = stream.getvalue()
    out = []
    while raw:
        size = int.from_bytes(raw[:4], "big")
        out.append(json.loads(raw[4 : 4 + size]))
        raw = raw[4 + size :]
    return out


def body(text="hello", response=None):
    contents = [{"role": "user", "parts": [{"text": text}]}]
    if response is not None:
        contents.append({"role": "model", "parts": [{"functionCall": {"name": "read_file"}}]})
        contents.append({"role": "user", "parts": [{"functionResponse": response}]})
    return {"contents": contents, "tools": TOOLS}


def test_launcher_runs_the_terminal_over_the_existing_gemini_transport(tmp_path, monkeypatch):
    cli = runpy.run_path(str(ROOT / "laptop/ccfleet"))
    scope = cli["cmd_gemini"].__globals__
    assert cli["NATIVE_TERMINAL_HASH"] == hashlib.sha256(
        Path(terminal.__file__).read_bytes()
    ).hexdigest()
    device = {
        "device_id": "test",
        "slot_id": "slot",
        "server": "https://ccfleet.io",
        "endpoint": "wss://ccfleet.io/api/cli/connect",
    }
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setenv("CCFLEET_HOME", str(tmp_path / "config"))

    def config():
        assert scope["home"]() == tmp_path / "config/antigravity-workspace"
        return {"devices": {"test": device}, "active": "test"}

    def transport(d, *, tty, fleet_only):
        assert d == device and not tty and fleet_only
        return ["paired-transport"]

    monkeypatch.setitem(scope, "load_config", config)
    monkeypatch.setitem(scope, "ssh_transport_command", transport)
    calls = []
    monkeypatch.setitem(
        scope, "packaged_helper", lambda n, h: SimpleNamespace(run=lambda *a: calls.append(a))
    )
    monkeypatch.setitem(scope, "installed_agent", lambda name: "/trusted/agy")
    args = SimpleNamespace(
        project=str(tmp_path), slot="", pair=False, preview=False, native=True
    )
    assert cli["cmd_gemini"](args) == 0
    assert calls == [
        ("/trusted/agy", tmp_path, ["paired-transport", "ccfleet-gemini-native-v1"])
    ]


def test_frame_roundtrip_and_limits():
    stream = packet({"ready": "ccfleet-native-v1"})
    assert terminal.receive(stream) == {"ready": "ccfleet-native-v1"}
    with pytest.raises(ValueError):
        terminal.frame(io.BytesIO(), {"x": "a" * terminal.LIMIT})


def test_select_tools_keeps_only_client_tools():
    decls = {
        "read_file": {"parametersJsonSchema": SCHEMA},
        "shell": {"parametersJsonSchema": SCHEMA},
    }
    assert terminal.select_tools(decls) == {"read_file": {"description": "", "parametersJsonSchema": SCHEMA}}


def test_mcp_schema_strips_the_tool_summary():
    cleaned = terminal.mcp_schema(SCHEMA)
    assert cleaned["properties"] == {"Path": {"type": "string"}}
    assert cleaned["required"] == ["Path"]


def test_complete_args_fills_the_review_fields_the_remote_cannot_know():
    command = {
        "parametersJsonSchema": {
            "properties": {"CommandLine": {"type": "string"}, "toolSummary": {"type": "string"}},
            "required": ["CommandLine", "toolSummary", "WaitMsBeforeAsync"],
        }
    }
    completed = terminal.complete_args("run_command", command, {"CommandLine": "git status"})
    assert completed["toolSummary"] == "Run Command: git status"
    assert completed["WaitMsBeforeAsync"] == 5000
    untouched = terminal.complete_args("read_file", {"parametersJsonSchema": {"required": ["Path"]}}, {"Path": "/x"})
    assert untouched == {"Path": "/x"}


def test_latest_user_text_ignores_model_and_function_turns():
    assert terminal.latest_user_text(body("first")) == "first"
    assert terminal.latest_user_text(body("", {"name": "read_file", "id": "c", "response": {}})) == ""


def test_mcp_reply_serves_initialize_list_and_call():
    tools = {"read_file": {"parametersJsonSchema": SCHEMA}}
    init = terminal.mcp_reply({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}, tools, lambda n, a: {})
    assert init["result"]["serverInfo"]["name"] == "client"
    listed = terminal.mcp_reply({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, tools, lambda n, a: {})
    assert listed["result"]["tools"][0]["inputSchema"]["required"] == ["Path"]
    called = terminal.mcp_reply(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_file", "arguments": {"Path": "/x", "toolSummary": "t"}}},
        tools,
        lambda name, args: {"output": args},
    )
    assert json.loads(called["result"]["content"][0]["text"]) == {"output": {"Path": "/x"}}
    assert terminal.mcp_reply({"jsonrpc": "2.0", "method": "ping"}, tools, lambda n, a: {}) is None
    refused = terminal.mcp_reply({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "shell"}}, tools, lambda n, a: {})
    assert refused["error"]["code"] == -32602


def test_bridge_validate_hello_rejects_bad_shapes():
    bridge = terminal.Bridge(io.BytesIO(), io.BytesIO(), "/bin/true", lambda: True)
    root, tools = bridge.validate_hello(
        {"hello": {"v": 1, "root": "/tmp/project", "tools": {"read_file": {"parametersJsonSchema": SCHEMA}}}}
    )
    assert root == "/tmp/project" and "read_file" in tools
    for bad in (
        {"hello": {"v": 2, "root": "/tmp", "tools": {"read_file": {"parametersJsonSchema": SCHEMA}}}},
        {"hello": {"v": 1, "root": "/tmp/../etc", "tools": {"read_file": {"parametersJsonSchema": SCHEMA}}}},
        {"hello": {"v": 1, "root": "/tmp", "tools": {"shell": {"parametersJsonSchema": SCHEMA}}}},
    ):
        with pytest.raises(ValueError):
            bridge.validate_hello(bad)


def test_only_bridged_and_benign_tools_are_allowed():
    assert terminal.remote_tool_allowed("mcp_client_read_file")
    assert terminal.remote_tool_allowed("send_message")
    assert not terminal.remote_tool_allowed("run_command")
    assert not terminal.remote_tool_allowed("view_file")
    assert not terminal.remote_tool_allowed("mcp_other_read_file")


def test_turn_reading_uses_the_recorded_transcript(tmp_path, monkeypatch):
    transcript = tmp_path / "brain" / "abc" / ".system_generated/logs/transcript_full.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE", "content": "from transcript"})
        + "\n"
        + json.dumps({"tool_calls": [{"name": "mcp_client_read_file", "args": {}}]})
        + "\n"
    )
    path = terminal.transcript_path("01234567-89ab-cdef-0123-456789abcdef")
    assert path is not None and path.name == "transcript_full.jsonl"
    assert terminal.transcript_path("../../etc") is None
    assert terminal.read_transcript(None) == []
    entries = terminal.read_transcript(transcript)
    assert terminal.turn_answer(entries) == "from transcript"
    assert terminal.turn_tool_names(entries) == {"mcp_client_read_file"}


def test_bridge_refuses_to_run_without_a_grant():
    with pytest.raises(ValueError, match="workspace grant"):
        terminal.Bridge(io.BytesIO(), io.BytesIO(), "/bin/true", lambda: False).run()


def test_bridge_transport_writes_prompts_and_delivers_results():
    class Proc:
        def __init__(self):
            self.stdin = io.BytesIO()

        def poll(self):
            return None

    bridge = terminal.Bridge(io.BytesIO(), io.BytesIO(), "/bin/true", lambda: True)
    bridge.proc = Proc()
    bridge.turn = "t" * 32
    bridge.call = "c" * 32
    stream = io.BytesIO()
    terminal.frame(stream, {"turn": "t" * 32, "call": "c" * 32, "result": {"output": "ok"}})
    terminal.frame(stream, {"bye": True})
    stream.seek(0)
    bridge.inp = stream
    bridge._transport()
    assert bridge.result == {"output": "ok"}


def test_bridge_transport_writes_the_prompt_to_the_agent():
    class Proc:
        def __init__(self):
            self.stdin = io.BytesIO()

        def poll(self):
            return None

    bridge = terminal.Bridge(io.BytesIO(), io.BytesIO(), "/bin/true", lambda: True)
    bridge.proc = Proc()
    stream = io.BytesIO()
    terminal.frame(stream, {"turn": "t" * 32, "prompt": "hello"})
    terminal.frame(stream, {"bye": True})
    stream.seek(0)
    bridge.inp = stream
    bridge._transport()
    assert json.loads(bridge.proc.stdin.getvalue()) == {
        "event": "user",
        "message": {"content": "hello"},
    }


def test_bridge_finish_sends_the_answer_and_clears_the_turn():
    bridge = terminal.Bridge(io.BytesIO(), io.BytesIO(), "/bin/true", lambda: True)
    bridge.turn = "t" * 32
    bridge._finish({"status": "SUCCESS", "response": "done"})
    assert frames(bridge.out) == [{"turn": "t" * 32, "answer": "done"}]
    assert bridge.turn is None


def test_session_auxiliary_request_gets_the_fixed_reply():
    session = terminal.Session(relay(), Path("/tmp"), Path("/tmp"), pump=False)
    mime, payload = session.dispatch(
        "/v1beta/models/gemini:streamGenerateContent?alt=sse", {"contents": []}
    )
    assert mime == "text/event-stream"
    assert terminal.AUX_TEXT in payload.decode()


def test_session_prompt_call_and_continuation(monkeypatch, tmp_path):
    monkeypatch.setattr(terminal.secrets, "token_hex", lambda size: "t" * 32)
    session = terminal.Session(relay(), tmp_path, tmp_path, pump=False)
    session.feed({"turn": "t" * 32, "call": "c" * 32, "name": "read_file", "args": {"Path": "/x"}})
    mime, payload = session.dispatch(
        "/v1beta/models/gemini:streamGenerateContent?alt=sse", body("hello")
    )
    assert "functionCall" in payload.decode()
    assert session.pending["call"] == "c" * 32
    assert session.turn_open == "t" * 32
    hello, prompt = frames(session.relay.stdin)
    assert hello["hello"]["root"] == str(tmp_path)
    assert "read_file" in hello["hello"]["tools"]
    assert prompt == {"turn": "t" * 32, "prompt": "hello"}
    session.relay.stdin.seek(0)
    session.relay.stdin.truncate()
    session.feed({"turn": "t" * 32, "answer": "done"})
    mime, payload = session.dispatch(
        "/v1beta/models/gemini:streamGenerateContent?alt=sse",
        body("hello", {"name": "read_file", "id": "c" * 32, "response": {"output": "ok"}}),
    )
    assert "done" in payload.decode()
    assert session.pending is None
    assert session.turn_open is None
    assert frames(session.relay.stdin) == [
        {"turn": "t" * 32, "call": "c" * 32, "result": {"output": "ok"}}
    ]


def test_session_replays_a_duplicate_request_without_new_frames(monkeypatch, tmp_path):
    monkeypatch.setattr(terminal.secrets, "token_hex", lambda size: "t" * 32)
    session = terminal.Session(relay(), tmp_path, tmp_path, pump=False)
    session.feed({"turn": "t" * 32, "call": "c" * 32, "name": "read_file", "args": {"Path": "/x"}})
    request = body("hello")
    first = session.dispatch("/v1beta/models/gemini:streamGenerateContent?alt=sse", request)
    size = len(session.relay.stdin.getvalue())
    second = session.dispatch("/v1beta/models/gemini:streamGenerateContent?alt=sse", request)
    assert first == second
    assert len(session.relay.stdin.getvalue()) == size


def test_session_refuses_an_unknown_tool_and_takes_the_next_event(monkeypatch, tmp_path):
    monkeypatch.setattr(terminal.secrets, "token_hex", lambda size: "t" * 32)
    session = terminal.Session(relay(), tmp_path, tmp_path, pump=False)
    session.feed({"turn": "t" * 32, "call": "c" * 32, "name": "shell", "args": {}})
    session.feed({"turn": "t" * 32, "answer": "recovered"})
    mime, payload = session.dispatch(
        "/v1beta/models/gemini:streamGenerateContent?alt=sse", body("hello")
    )
    assert "recovered" in payload.decode()
    assert "unsupported" in session.relay.stdin.getvalue().decode()


def test_session_releases_an_interrupted_call_before_the_next_prompt(monkeypatch, tmp_path):
    monkeypatch.setattr(terminal.secrets, "token_hex", lambda size: "t" * 32)
    session = terminal.Session(relay(), tmp_path, tmp_path, pump=False)
    session.feed({"turn": "t" * 32, "call": "c" * 32, "name": "read_file", "args": {"Path": "/x"}})
    session.dispatch("/v1beta/models/gemini:streamGenerateContent?alt=sse", body("hello"))
    session.relay.stdin.seek(0)
    session.relay.stdin.truncate()
    session.feed({"turn": "t" * 32, "answer": "interrupted"})
    session.feed({"turn": "t" * 32, "call": "d" * 32, "name": "read_file", "args": {"Path": "/y"}})
    mime, payload = session.dispatch(
        "/v1beta/models/gemini:streamGenerateContent?alt=sse", body("again")
    )
    assert "functionCall" in payload.decode()
    released = frames(session.relay.stdin)
    assert released[0] == {
        "turn": "t" * 32,
        "call": "c" * 32,
        "error": "The local operation was interrupted before it finished.",
    }
    assert released[1]["prompt"] == "again"
    assert session.pending["call"] == "d" * 32


def test_session_releases_a_denied_call_from_the_transcript(tmp_path):
    transcript = tmp_path / ".gemini/antigravity-cli/brain/turn/.system_generated/logs/transcript_full.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps({"step_index": 1, "tool_calls": [{"name": "read_file", "args": {"Path": "/x"}}]})
        + "\n"
        + json.dumps({"step_index": 2, "status": "ERROR", "error": "The user denied permission."})
        + "\n"
    )
    session = terminal.Session(relay(), tmp_path, tmp_path, pump=False)
    pending = {
        "turn": "t" * 32,
        "call": "c" * 32,
        "name": "read_file",
        "args": {"Path": "/x"},
        "at": time.time() - 1,
    }
    assert session._denial(pending) == "The user denied permission."
    session.pending = pending
    session._release("c" * 32, "The user denied permission.")
    assert frames(session.relay.stdin) == [
        {"turn": "t" * 32, "call": "c" * 32, "error": "The user denied permission."}
    ]


def test_endpoint_requires_the_loopback_key():
    session = terminal.Session(relay(), Path("/tmp"), Path("/tmp"), pump=False)
    with terminal.endpoint(session.dispatch) as (url, key):
        port = int(url.rsplit(":", 1)[1])
        bad = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        bad.request("POST", "/v1beta/models/gemini:streamGenerateContent?alt=sse", body=b"{}")
        assert bad.getresponse().status == 400
        good = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        good.request(
            "POST",
            "/v1beta/models/gemini:streamGenerateContent?alt=sse",
            body=json.dumps({"contents": []}),
            headers={"X-Goog-Api-Key": key},
        )
        reply = good.getresponse()
        assert reply.status == 200
        assert terminal.AUX_TEXT in reply.read().decode()
