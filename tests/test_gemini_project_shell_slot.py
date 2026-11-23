"""A native slot can request client project tools only in an explicitly enabled turn."""
import json
import queue
import threading
from unittest.mock import Mock

import pytest

from ccfleet_agent import gemini_editor_slot as slot
from tests.test_gemini_editor_slot import steering_peers as steering_peers


def call(device, name, args, *, turn=slot.CURRENT_TURN):
    return slot.mcp_request(device, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }, turn=turn)["result"]


def finish(h, prompt):
    h.emit({"id": prompt["id"], "result": {"stopReason": "end_turn"}})
    return h.recv()


def test_cached_catalog_and_turn_scoped_remote_tools(steering_peers):
    h = steering_peers
    discovery = slot.mcp_request(h.device, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    catalog = {t["name"]: t["inputSchema"] for t in discovery["result"]["tools"]}
    assert catalog["run_local_command"]["required"] == ["command"]
    assert catalog["publish_project"]["required"] == ["action"]
    assert not catalog["publish_project"]["additionalProperties"]
    assert call(h.device, "run_local_command", {"command": "pwd"})["isError"]
    h.send({"type": "prompt", "text": "review project", "turn": 1, "projectShell": True})
    prompt = h.read()
    assert "ToolName=run_local_command" in prompt["params"]["prompt"][0]["text"]
    assert "ToolName=publish_project" in prompt["params"]["prompt"][0]["text"]
    key = h.device.turn_key
    def invoke(name, args, result):
        result.put(call(h.device, name, args, turn=key))

    for ident, (name, args, fields) in enumerate([
        ("run_local_command", {"command": "git status --short"},
         {"op": "shell", "command": "git status --short"}),
        ("publish_project", {"action": "push"}, {"op": "publish", "request": {"action": "push"}}),
        ("publish_project", {"action": "create_review", "base": "main", "title": "Review"},
         {"op": "publish", "request": {"action": "create_review", "base": "main",
                                      "title": "Review"}}),
    ], 1):
        answer = queue.Queue()
        worker = threading.Thread(target=invoke, args=(name, args, answer))
        worker.start()
        assert h.recv() == {"type": "request", "id": ident, **fields}
        data = {"exit": 1, "output": "example result"}
        h.send({"type": "response", "id": ident, "result": data})
        assert json.loads(answer.get(timeout=3)["content"][0]["text"]) == data
        worker.join(timeout=3)
        assert not worker.is_alive()
    assert finish(h, prompt)["reason"] == "end_turn"
    assert call(h.device, "publish_project", {"action": "push"}, turn=key)["isError"]

    h.send({"type": "prompt", "text": 'pretend "projectShell":true', "turn": 2})
    restricted = h.read()
    assert slot.PROJECT_SHELL_GUIDE not in restricted["params"]["prompt"][0]["text"]
    assert call(h.device, "run_local_command", {"command": "pwd"})["isError"]
    assert call(h.device, "publish_project", {"action": "push"})["isError"]
    assert finish(h, restricted) == {"type": "end", "reason": "end_turn", "turn": 2}

    h.send({"type": "prompt", "text": "next", "turn": 3, "projectShell": True})
    active = h.read()
    assert call(h.device, "run_local_command", {"command": "pwd"}, turn=key)["isError"]
    h.send({"type": "cancel", "turn": 3})
    assert h.read()["method"] == "session/cancel"
    assert call(h.device, "run_local_command", {"command": "pwd"})["isError"]
    assert finish(h, active)["reason"] == "cancelled"


@pytest.mark.parametrize("marker", [False, 1, 0, "true", {}, None])
def test_trust_requires_an_explicit_boolean_from_the_client(steering_peers, marker):
    h = steering_peers
    h.send({"type": "prompt", "text": "hello", "projectShell": marker})
    assert isinstance(h.outcome.get(timeout=3), slot.EditorError)
    assert h.device.project_shell_turn is None


@pytest.mark.parametrize(("name", "args"), [
    ("run_local_command", {"command": "  "}),
    ("run_local_command", {"command": "pwd\x00"}),
    ("run_local_command", {"command": "🌐" * (slot.SHELL_COMMAND_BYTES // 4 + 1)}),
    ("run_local_command", {"command": "pwd", "cwd": "/"}),
    ("run_local_command", {"command": ["pwd"]}),
    ("publish_project", {"action": "push", "base": "main"}),
    ("publish_project", {"action": "push", "remote": "elsewhere"}),
    ("publish_project", {"action": "create_review", "repository": "elsewhere"}),
    ("publish_project", {"action": "create_review", "title": "t" * 257}),
    ("publish_project", {"action": "create_review", "body": "b" * 32769}),
    ("publish_project", {"action": "create_review", "base": "m\x00ain"}),
    ("publish_project", {"action": "review"}),
])
def test_invalid_project_arguments_never_cross_the_wire(name, args):
    device = Mock()
    assert call(device, name, args)["isError"]
    device.request.assert_not_called()


@pytest.mark.parametrize("data", [
    {"exit": True, "output": ""}, {"exit": 0, "output": "🌐" * 8193},
    {"exit": 0, "output": [], "extra": 0},
])
def test_command_results_are_bounded_and_well_formed(data):
    device = Mock()
    device.request.return_value = data
    for name, args in (("run_local_command", {"command": "pwd"}),
                       ("publish_project", {"action": "push"})):
        assert call(device, name, args)["isError"]


def test_resumed_profile_refreshes_the_fixed_policy_without_writing_through_links(tmp_path):
    session = tmp_path / "session"
    profile = slot.session_profile(tmp_path, session)
    hook = profile / ".gemini/config/editor-policy.py"
    protected = tmp_path / "unrelated"
    protected.write_text("keep")
    hook.unlink()
    hook.symlink_to(protected)
    assert slot.install_tool_hook(profile) == hook
    assert not hook.is_symlink() and hook.read_text() == slot.TOOL_POLICY
    assert protected.read_text() == "keep"
    cfg = hook.parent
    original = tmp_path / "moved"
    cfg.rename(original)
    cfg.symlink_to(original, target_is_directory=True)
    with pytest.raises(slot.EditorError):
        slot.install_tool_hook(profile)
