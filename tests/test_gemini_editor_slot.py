"""Slot-side interactive editor contract and isolation boundaries."""

import hashlib
import io
import json
import queue
import socket
import threading
from unittest.mock import Mock

import pytest

from ccfleet_agent import gemini_editor_slot as slot


@pytest.fixture
def agent(tmp_path):
    a = slot.Agent.__new__(slot.Agent)
    a.root = tmp_path
    a.sid = "session"
    a.stdin_lock = threading.Lock()
    a.prompt_turn = None
    a.device = Mock()
    a.approvals = {}
    a.active = threading.Event()
    a.active.set()
    return a


def permission(a, tool):
    return a.callback(
        "session/request_permission",
        {
            "sessionId": a.sid,
            "toolCall": tool,
            "options": [
                {"kind": "allow_once", "optionId": "yes"},
                {"kind": "reject_once", "optionId": "no"},
            ],
        },
    )["outcome"]["optionId"]


def write(a, path, text):
    return a.callback(
        "fs/write_text_file", {"sessionId": a.sid, "path": str(a.root / path), "content": text}
    )


def change(a, path, text):
    return {
        "kind": "edit",
        "title": "Run client_edit_file?",
        "content": [
            {
                "type": "diff",
                "path": str(a.root / path),
                "oldText": "untrusted slot copy",
                "newText": text,
            }
        ],
    }


def test_one_use_edit_approval(agent):
    agent.device.request.return_value = True
    assert permission(agent, change(agent, "example.txt", "new")) == "yes"
    agent.device.request.assert_called_once_with("approve", path="example.txt", content="new")
    with pytest.raises(ValueError):
        write(agent, "example.txt", "other")
    with pytest.raises(ValueError):
        write(agent, "example.txt", "new")
    assert permission(agent, change(agent, "example.txt", "new")) == "yes"
    assert write(agent, "example.txt", "new") == {}
    with pytest.raises(ValueError):
        write(agent, "example.txt", "new")


@pytest.mark.parametrize(
    "tool",
    [
        {"kind": "execute", "title": "Run command"},
        {"kind": "edit", "title": "Apply patch on host"},
        {"kind": "other", "_meta": {"mcp": {"server": "user-chosen", "tool": "test_project"}}},
        {"kind": "other", "_meta": {"mcp": {"server": slot.MCP_NAME, "tool": "run_command"}}},
    ],
)
def test_host_and_unregistered_tools_denied(agent, tool):
    assert permission(agent, tool) == "no"
    agent.device.request.assert_not_called()


def test_file_scope_session_and_ranges(agent):
    agent.device.request.return_value = "first\nsecond\nthird"
    pars = {"sessionId": agent.sid, "path": str(agent.root / "a.txt"), "line": 2, "limit": 1}
    assert agent.callback("fs/read_text_file", pars) == {"content": "second\n"}
    for invalid in ("../outside", ".git/config", "sub/../../file"):
        with pytest.raises(ValueError):
            agent.callback("fs/read_text_file", {**pars, "path": str(agent.root / invalid)})
    with pytest.raises(ValueError):
        agent.callback("fs/read_text_file", {**pars, "sessionId": "other"})
    agent.active.clear()
    with pytest.raises(ValueError):
        agent.callback("fs/read_text_file", pars)


def test_only_gemini_selected(agent):
    agent.rpc = Mock(
        side_effect=[
            {
                "sessionId": "s",
                "models": {
                    "currentModelId": "claude-opus",
                    "availableModels": [{"modelId": "claude-opus"}, {"modelId": "gemini-test"}],
                },
            },
            {},
        ]
    )
    assert agent.session(agent.root, []) == "gemini-test"
    assert agent.rpc.call_args.args[1]["modelId"] == "gemini-test"
    agent.rpc.side_effect = [
        {"sessionId": "s", "models": {"availableModels": [{"modelId": "claude"}]}}
    ]
    with pytest.raises(slot.EditorError):
        agent.session(agent.root, [])


def test_model_menu_uses_one_acp_session_and_no_inference_for_commands(agent):
    agent.active.clear()
    agent.seq = 0
    agent.process = Mock(stdin=io.BytesIO())
    agent.messages = queue.Queue()
    model_a, model_b = "gemini-test-low", "gemini-test-high"
    offered = [
        {"modelId": model_a, "name": "Not sent", "_meta": {"account": "private"}},
        {"modelId": "claude-private"},
        {"modelId": model_b},
        {"modelId": model_a},
        {"modelId": "gemini-invalid\nid"},
        {"modelId": None},
    ]
    for msg in [
        {
            "id": 1,
            "result": {
                "sessionId": "same",
                "models": {"currentModelId": model_a, "availableModels": offered},
            },
        },
        {"id": 2, "result": {}},
        {"id": 3, "error": {"code": -32602, "message": "private error from upstream"}},
        {"id": 4, "result": {}},
        {"id": 5, "result": {"stopReason": "end_turn"}},
    ]:
        agent.messages.put(msg)

    agent.session(agent.root, [])
    agent.device.receive.side_effect = [
        {"type": "set_model", "model": model_a},
        {"type": "set_model", "model": "claude-private"},
        {"type": "set_model", "model": model_b},
        {"type": "set_model", "model": model_b},
        {"type": "prompt", "text": "A new user message"},
        {"type": "close"},
    ]
    slot.interact(agent, agent.device)

    replies = [c.args[0] for c in agent.device.send.call_args_list]
    assert replies == [
        {"type": "ready", "model": model_a, "models": [model_a, model_b],
         "capabilities": {"cancelTurn": True, "projectShell": True}},
        {"type": "model", "model": model_a},
        {"type": "model", "model": model_a, "error": "Choose a Gemini model offered by this slot."},
        {
            "type": "model",
            "model": model_a,
            "error": "The Google editor declined the model change.",
        },
        {"type": "model", "model": model_b},
        {"type": "end", "reason": "end_turn"},
    ]
    sent = [json.loads(b) for b in agent.process.stdin.getvalue().splitlines()]
    assert [m["method"] for m in sent] == [
        "session/new",
        "session/set_model",
        "session/set_model",
        "session/set_model",
        "session/prompt",
    ]
    assert [m["params"]["sessionId"] for m in sent[1:]] == ["same"] * 4
    assert [m["params"]["modelId"] for m in sent[1:-1]] == [model_a, model_b, model_b]
    assert agent.model == model_b
    agent.device.request.assert_not_called()


def test_switch_without_upstream_confirmation_terminates_session(agent):
    agent.model = "gemini-a"
    agent.models = ["gemini-a", "gemini-b"]
    agent.device.receive.return_value = {"type": "set_model", "model": "gemini-b"}
    agent.seq = 0
    agent.messages = Mock()
    agent.messages.get.side_effect = queue.Empty()
    agent.process = Mock(stdin=io.BytesIO())

    with pytest.raises(slot.EditorError, match="timed out"):
        slot.interact(agent, agent.device)
    agent.device.send.assert_called_once_with(
        {"type": "ready", "model": "gemini-a", "models": ["gemini-a", "gemini-b"],
         "capabilities": {"cancelTurn": True, "projectShell": True}}
    )
    agent.device.request.assert_not_called()


def test_model_catalog_is_bounded(agent):
    agent.rpc = Mock(
        side_effect=[
            {
                "sessionId": "s",
                "models": {
                    "availableModels": [
                        {"modelId": f"gemini-{i}"} for i in range(slot.MAX_MODELS + 20)
                    ]
                },
            },
            {},
        ]
    )
    agent.session(agent.root, [])
    assert len(agent.models) == slot.MAX_MODELS
    assert agent.model == "gemini-0"


@pytest.mark.parametrize(
    ("name", "wanted"),
    [
        (".", "."),
        ("./", "."),
        ("./src/", "src"),
        ("/virtual/project/", "."),
        ("/virtual/project/src/", "src"),
        ("/virtual/project-other", None),
        ("/Users/example", None),
        ("/virtual/project/../project", None),
        ("../other", None),
        ("src/.git/", None),
        (1, None),
    ],
)
def test_list_project_accepts_virtual_project_paths_only(name, wanted):
    from pathlib import Path

    device = Mock()
    device.request.return_value = ["sample.txt"]
    reply = slot.mcp_request(
        device,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_project", "arguments": {"path": name}},
        },
        Path("/virtual/project"),
    )["result"]
    if wanted is None:
        device.request.assert_not_called()
        assert reply["isError"]
        assert "project-relative" in reply["content"][0]["text"]
    else:
        device.request.assert_called_once_with("list", path=wanted)
        assert json.loads(reply["content"][0]["text"]) == ["sample.txt"]


def test_mcp_discovery_and_test_boundary():
    device = Mock()

    def call(method, params=None):
        return slot.mcp_request(
            device, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        )

    assert call("optional-preflight")["error"]["code"] == -32601
    assert call("resources/templates/list")["result"] == {"resourceTemplates": []}
    assert {v["name"] for v in call("tools/list")["result"]["tools"]} == slot.MCP_TOOLS.keys()
    assert call("tools/call", {"name": "test_project", "arguments": {"command": "npm install"}})[
        "result"
    ]["isError"]
    device.request.assert_not_called()
    device.request.return_value = {"output": "hello", "exit": 0}
    result = call("tools/call", {"name": "test_project", "arguments": {}})
    assert json.loads(result["result"]["content"][0]["text"])["exit"] == 0
    device.request.assert_called_once_with("test")


@pytest.mark.parametrize("auth", ["gemini-api-key", "oauth-business", "agent-platform", None])
def test_personal_profile_only(tmp_path, auth):
    profile = tmp_path / ".gemini/antigravity-acp"
    profile.mkdir(parents=True)
    (profile / "settings.json").write_text(json.dumps({"auth": {"type": auth}}))
    with pytest.raises(slot.EditorError):
        slot.require_personal(tmp_path)
    (profile / "settings.json").write_text('{"auth":{"type":"oauth-personal"}}')
    slot.require_personal(tmp_path)


def test_disconnect_and_revocation_abort_device():
    for close_client in (True, False):
        left, right = socket.socketpair()
        device = slot.Device(left, lambda active=close_client: active)
        device.start()
        try:
            if close_client:
                right.close()
            assert device.closed.wait(3)
            with pytest.raises(EOFError):
                device.receive(1)
        finally:
            device.abort()
            right.close()
            left.close()


def test_refuse_arbitrary_injected_ssh_operation(monkeypatch):
    from ccfleet_agent import antigravity_entry as entry

    monkeypatch.setattr(entry.sys, "platform", "darwin")
    monkeypatch.setattr(entry.sys, "argv", ["slot", hashlib.sha256(b"fixture").hexdigest()])
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "ccfleet-gemini-editor-v1 /usr/bin/id")
    monkeypatch.setattr(entry.os, "isatty", lambda fd: False)
    monkeypatch.setattr(
        entry.mac_policy,
        "authenticated_owner",
        lambda: pytest.fail("unexpected authorization lookup"),
    )
    assert entry.main() == 2


def test_remote_cannot_register_mcp_or_start_login(agent):
    for method in ("authenticate", "session/new", "terminal/create", "_private/open_url"):
        with pytest.raises(ValueError):
            agent.callback(method, {"sessionId": agent.sid, "path": str(agent.root / "x")})


@pytest.mark.parametrize(
    ("name", "args", "expected"),
    [
        ("client_view_file", {}, "allow"),
        ("client_create_file", {}, "allow"),
        ("client_edit_file", {}, "allow"),
        ("call_mcp_tool", {"ServerName": "ccfleetlocal", "ToolName": "test_project"}, "allow"),
        ("call_mcp_tool", {"ServerName": "elsewhere", "ToolName": "test_project"}, "deny"),
        ("call_mcp_tool", {"ServerName": "ccfleetlocal", "ToolName": "run_local_command"}, "allow"),
        ("call_mcp_tool", {"ServerName": "ccfleetlocal", "ToolName": "publish_project"}, "allow"),
        ("run_local_command", {"ServerName": "elsewhere", "ToolName": "run_local_command"}, "deny"),
        ("run_command", {"ServerName": "ccfleetlocal", "ToolName": "run_local_command"}, "deny"),
        ("test_project", {"ServerName": "ccfleetlocal", "ToolName": "test_project"}, "allow"),
        ("list_project", {"ServerName": "ccfleetlocal", "ToolName": "list_project"}, "allow"),
        ("list_project", {"ServerName": "ccfleetlocal", "ToolName": "test_project"}, "deny"),
        ("test_project", {}, "deny"),
        ("run_command", {}, "deny"),
        ("view_file", {}, "deny"),
        ("read_url_content", {}, "deny"),
        ("search_web", {}, "deny"),
        ("generate_image", {}, "deny"),
        ("invoke_subagent", {}, "deny"),
        ("schedule", {}, "deny"),
        ("run_workflow", {}, "deny"),
        ("future_tool", {}, "deny"),
    ],
)
def test_google_pretool_policy(name, args, expected):
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-I", "-B", "-c", slot.TOOL_POLICY],
        input=json.dumps({"toolCall": {"name": name, "args": args}}),
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(proc.stdout)["decision"] == expected


def test_profile_uses_only_personal_keychain_and_fixed_hook(tmp_path):
    home = tmp_path / "owner"
    home.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    profile = slot.session_profile(home, scratch)
    cfg = profile / ".gemini/config"
    assert (profile / "Library/Keychains").readlink() == home / "Library/Keychains"
    assert json.loads((profile / ".gemini/antigravity-acp/settings.json").read_text()) == {
        "auth": {"type": "oauth-personal"}
    }
    hooks = json.loads((cfg / "hooks.json").read_text())["ccfleet-editor"]
    assert hooks["enabled"] is True and len(hooks["PreToolUse"]) == 1
    assert (cfg / "editor-policy.py").read_text() == slot.TOOL_POLICY
    assert str(cfg / "editor-policy.py") in hooks["PreToolUse"][0]["hooks"][0]["command"]


@pytest.fixture
def steering_peers(tmp_path):
    """Real framed workspace and ACP pipes, independent of any account or model."""
    from contextlib import ExitStack
    from types import SimpleNamespace

    with ExitStack() as stack:
        device_socket, mac = socket.socketpair()
        app, provider = socket.socketpair()
        for sock in (device_socket, mac, app, provider):
            stack.enter_context(sock)
        provider.settimeout(3)
        mac.settimeout(3)
        device = slot.Device(device_socket, lambda: True)
        device.start()
        p = SimpleNamespace(stdin=stack.enter_context(app.makefile("wb")),
                            stdout=stack.enter_context(app.makefile("rb")))
        agent = slot.Agent(p, device, tmp_path)
        agent.sid = "same-google-conversation"
        agent.model, agent.models = "gemini-test", ["gemini-test"]
        mr, mw = (stack.enter_context(mac.makefile(mode)) for mode in ("rb", "wb"))
        pr, pw = (stack.enter_context(provider.makefile(mode)) for mode in ("rb", "wb"))
        outcome = queue.Queue()

        def interact():
            try:
                slot.interact(agent, device)
            except (ValueError, OSError, EOFError) as exc:
                outcome.put(exc)
            else:
                outcome.put(None)

        worker = threading.Thread(target=interact, daemon=True)
        worker.start()
        assert slot.gemini_preview.receive(mr)["capabilities"]["cancelTurn"] is True
        h = SimpleNamespace(
            send=lambda msg: slot.gemini_preview.send(mw, msg),
            recv=lambda: slot.gemini_preview.receive(mr),
            emit=lambda msg: slot.send_line(pw, {"jsonrpc": "2.0", **msg}),
            read=lambda: slot.json_line(pr), device=device, mac=mac, agent=agent, outcome=outcome,
        )
        try:
            yield h
        finally:
            device.abort()
            mac.close()
            provider.shutdown(socket.SHUT_RDWR)
            worker.join(timeout=3)
            device.input.close()
            device.output.close()
            assert not worker.is_alive()


def test_steering_keeps_session_rejects_late_edits_and_answers_blocked_permission(steering_peers):
    h = steering_peers
    sid = h.agent.sid

    def ask(ident, content):
        h.emit({"id": ident, "method": "session/request_permission", "params": {
            "sessionId": sid, "toolCall": change(h.agent, content, content),
            "options": [{"kind": "allow_once", "optionId": "yes"},
                        {"kind": "reject_once", "optionId": "no"}],
        }})
        req = h.recv()
        assert req["op"] == "approve" and req["content"] == content
        return req["id"]

    def stale_write(ident):
        h.emit({"id": ident, "method": "fs/write_text_file", "params": {
            "sessionId": sid, "path": str(h.agent.root / "before"), "content": "before"
        }})
        assert h.read()["error"]["code"] == -32602

    h.send({"type": "prompt", "text": "first instruction", "turn": 1})
    first = h.read()
    assert first["method"] == "session/prompt" and first["params"]["sessionId"] == sid
    before = ask(100, "before")
    h.send({"type": "response", "id": before, "result": True})
    assert h.read()["result"]["outcome"]["optionId"] == "yes"

    blocked = ask(101, "blocked")
    h.send({"type": "cancel", "turn": 1})
    # The Google interrupt reaches the agent even before the local callback has replied.
    assert h.read() == {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}}
    h.send({"type": "response", "id": blocked, "error": "declined"})
    assert h.read()["result"]["outcome"] == {"outcome": "selected", "optionId": "no"}
    stale_write(102)
    h.emit({"method": "session/update", "params": {"sessionId": sid, "update": {
        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "late"}}}})
    h.emit({"id": first["id"], "result": {"stopReason": "cancelled"}})
    assert h.recv() == {"type": "end", "reason": "cancelled", "turn": 1}

    h.send({"type": "prompt", "text": "show a preview", "turn": 2})
    second = h.read()
    assert second["method"] == "session/prompt" and second["params"]["sessionId"] == sid
    assert second["params"]["prompt"][0]["text"].endswith("show a preview")
    h.send({"type": "cancel", "turn": 1})  # Late duplicate cannot interrupt turn 2.
    stale_write(103)
    h.emit({"method": "session/update", "params": {"sessionId": sid, "update": {
        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "preview"}}}})
    h.emit({"id": second["id"], "result": {"stopReason": "end_turn"}})
    assert h.recv() == {"type": "text", "text": "preview"}
    assert h.recv() == {"type": "end", "reason": "end_turn", "turn": 2}
    h.send({"type": "close"})
    assert h.outcome.get(timeout=3) is None


def test_cancelled_before_dispatch_sends_no_upstream_prompt(agent):
    agent.seq = 0
    agent.process = Mock(stdin=io.BytesIO())
    agent.device.cancelled_turn = 1
    assert agent.rpc("session/prompt", {}, turn=1) == {"stopReason": "cancelled"}
    assert agent.process.stdin.getvalue() == b""


def test_end_barrier_drains_mcp_callback_and_refuses_previous_turn(steering_peers):
    import select

    h = steering_peers
    h.send({"type": "prompt", "text": "old", "turn": 1})
    first = h.read()
    key = h.device.turn_key
    result = queue.Queue()

    def pending():
        try:
            result.put(h.device.request("list", turn=key, path="."))
        except ValueError as exc:
            result.put(exc)

    worker = threading.Thread(target=pending, daemon=True)
    worker.start()
    req = h.recv()
    assert req["op"] == "list"
    h.send({"type": "cancel", "turn": 1})
    assert h.read()["method"] == "session/cancel"
    # Even if the provider completes before its independent MCP callback, T3 must not start
    # its next turn until the old callback's response has been consumed from this channel.
    h.emit({"id": first["id"], "result": {"stopReason": "end_turn"}})
    assert not select.select([h.mac], [], [], 0.05)[0]
    h.send({"type": "response", "id": req["id"], "error": "cancelled"})
    assert isinstance(result.get(timeout=3), ValueError)
    worker.join(timeout=2)
    assert h.recv() == {"type": "end", "reason": "cancelled", "turn": 1}

    h.send({"type": "prompt", "text": "next", "turn": 2})
    second = h.read()
    with pytest.raises(ValueError, match="no active turn"):
        h.device.request("mkdir", turn=key, path="late-folder")
    h.emit({"id": second["id"], "result": {"stopReason": "end_turn"}})
    assert h.recv()["turn"] == 2
    h.send({"type": "close"})
    assert h.outcome.get(timeout=3) is None


def test_unresponsive_agent_never_acknowledges_cancelled_turn(steering_peers, monkeypatch):
    h = steering_peers
    monkeypatch.setattr(slot, "CANCEL_GRACE", 0.15)
    h.send({"type": "prompt", "text": "first", "turn": 1})
    assert h.read()["method"] == "session/prompt"
    h.send({"type": "cancel", "turn": 1})
    assert h.read()["method"] == "session/cancel"
    assert isinstance(h.outcome.get(timeout=3), (slot.EditorError, EOFError))


def test_old_client_still_uses_untagged_prompt_and_end_frames(steering_peers):
    h = steering_peers
    h.send({"type": "prompt", "text": "legacy client"})
    first = h.read()
    assert first["method"] == "session/prompt"
    h.emit({"method": "session/update", "params": {"sessionId": h.agent.sid, "update": {
        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hello"}}}})
    h.emit({"id": first["id"], "result": {"stopReason": "end_turn"}})
    assert h.recv() == {"type": "text", "text": "hello"}
    assert h.recv() == {"type": "end", "reason": "end_turn"}
    h.send({"type": "close"})
    assert h.outcome.get(timeout=3) is None
