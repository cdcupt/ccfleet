"""Slot-side interactive editor contract and isolation boundaries."""

import hashlib
import json
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
