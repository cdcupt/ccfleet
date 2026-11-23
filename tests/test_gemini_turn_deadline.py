"""A running Gemini turn uses an inactivity deadline; a silent one still ends."""
import io
import queue
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ccfleet_agent import gemini_editor_slot as slot


def rpc(monkeypatch, events, *, method="session/prompt", peer_supports_progress=True):
    now = [0.0]
    monkeypatch.setattr(slot, "time", SimpleNamespace(monotonic=lambda: now[0]))
    events = iter(events)

    def receive(*, timeout):
        delay, frame = next(events)
        if delay > timeout:
            now[0] += timeout
            raise queue.Empty
        now[0] += delay
        return frame

    agent = slot.Agent.__new__(slot.Agent)
    agent.seq = 0
    agent.sid = "test-session"
    agent.stdin_lock = threading.Lock()
    agent.active = threading.Event()
    agent.active.set()
    agent.device = SimpleNamespace(cancelled_turn=None, resume_enabled=peer_supports_progress,
                                   send=Mock())
    agent.messages = SimpleNamespace(get=receive)
    agent.process = SimpleNamespace(stdin=io.BytesIO())
    agent.rpc(method, {}, timeout=900, turn=1)
    return [c.args[0] for c in agent.device.send.call_args_list]


def update(event, session="test-session", **fields):
    return {"method": "session/update", "params": {
        "sessionId": session, "update": {"sessionUpdate": event, **fields},
    }}


DONE = {"id": 1, "result": {"stopReason": "end_turn"}}


@pytest.mark.parametrize("negotiated", [True, False])
def test_active_turn_can_exceed_fifteen_minutes(monkeypatch, negotiated):
    private = {"type": "text", "text": "fixture thought"}
    public = {"type": "text", "text": "working"}
    frames = rpc(monkeypatch, [
        (600, update("agent_message_chunk", content=public)),
        (600, update("agent_thought_chunk", content=private)),
        (5, update("tool_call_update", toolCallId="tool-1")),
        (800, DONE),
    ], peer_supports_progress=negotiated)
    expected = [{"type": "text", "text": "working"}]
    if negotiated:
        expected.append({"type": "progress", "turn": 1})
    assert frames == expected


@pytest.mark.parametrize("msg", [
    {"method": "unrelated"},
    update("agent_thought_chunk", "other", content={"type": "text", "text": "noise"}),
    update("agent_message_chunk", content={"type": "text", "text": ""}),
    update("tool_call_update", toolCallId=""),
])
def test_no_progress_still_times_out(monkeypatch, msg):
    with pytest.raises(slot.EditorError, match="timed out"):
        rpc(monkeypatch, [(700, msg), (201, DONE)])


def test_initialization_keeps_absolute_timeout(monkeypatch):
    with pytest.raises(slot.EditorError, match="timed out"):
        rpc(monkeypatch, [(700, update("tool_call", toolCallId="one")), (201, DONE)],
            method="initialize")


def test_provider_internal_error_ends_turn_without_replaying_or_discarding_session():
    agent = slot.Agent.__new__(slot.Agent)
    agent.sid, agent.seq = "same-provider-session", 0
    agent.model, agent.models = "gemini-a", ["gemini-a"]
    agent.active, agent.stdin_lock = threading.Event(), threading.Lock()
    agent.approvals = {"old": "unusable"}
    agent.process = SimpleNamespace(stdin=io.BytesIO())
    agent.messages = queue.Queue()
    agent.messages.put({"id": 1, "error": {"code": -32603, "message": "PRIVATE-vendor-detail"}})
    agent.messages.put({"id": 2, "result": {"stopReason": "end_turn"}})
    dev = Mock(cancelled_turn=None)
    dev.receive.side_effect = [{"type": "prompt", "text": "first", "turn": 1},
                               {"type": "prompt", "text": "second", "turn": 2},
                               {"type": "close"}]
    agent.device = dev
    slot.interact(agent, dev)
    sent = [c.args[0] for c in dev.send.call_args_list]
    assert [m['reason'] for m in sent if m['type'] == 'end'] == ["refusal", "end_turn"]
    assert "PRIVATE-vendor-detail" not in repr(sent) and not agent.approvals
    assert len(agent.process.stdin.getvalue().splitlines()) == 2


@pytest.mark.parametrize(("active", "packet_at", "at_end"), [
    (False, 200, 180), (True, 600, 600), (True, 1100, 900),
])
def test_provider_transport_respects_active_turn_budget(monkeypatch, active, packet_at, at_end):
    now = [0.0]
    handler = slot.GoogleConnection.__new__(slot.GoogleConnection)
    handler.server = SimpleNamespace(active_prompt=threading.Event())
    handler.request = object()
    if active:
        handler.server.active_prompt.set()
    peer = object()

    def wait(read, write, errors, timeout):
        assert read == [handler.request, peer] and not write and not errors
        assert 0 < timeout <= 180
        now[0] = min(packet_at, now[0] + timeout)
        return ([peer] if now[0] == packet_at else [], [], [])

    monkeypatch.setattr(slot, "select", SimpleNamespace(select=wait))
    monkeypatch.setattr(slot, "time", SimpleNamespace(monotonic=lambda: now[0]))
    assert handler._readable(peer) == ([peer] if at_end == packet_at else [])
    assert now[0] == at_end
