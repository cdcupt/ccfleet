"""Bounded owned text buffering with synthetic peers; no native/model calls."""

import copy
import time

import pytest

from ccfleet_agent import devin_acp as acp
from tests.test_devin_acp import fake as fake


class TextController(acp.ACPController):
    def _queue_notification(self, params, *, provisional=False):
        if not provisional and self._bounded_text_notification(params):
            return
        super()._queue_notification(params, provisional=provisional)


def notice(kind="agent_message_chunk", text="synthetic", session="owned", **extra):
    return {"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session,
        "update": {"sessionUpdate": kind, "content": {"type": "text", "text": text}}, **extra}}


def handle(controller, value):
    controller._handle(value, time.monotonic() + 1)


@pytest.fixture
def owned(fake):
    make, _, _ = fake
    controller = make(allow_model_requests=True, _controller_type=TextController)
    controller._sessions.add("owned")
    yield controller
    assert controller.pid is None
    controller.close()


@pytest.mark.parametrize("mode,fragments", [
    ("stream-text-129", 129), ("stream-text-1024", 1024), ("stream-mixed-512", 512),
])
def test_text_and_private_thought_streams_cross_original_queue_bound(fake, mode, fragments):
    make, record, _ = fake
    with make(mode, allow_model_requests=True, _controller_type=TextController) as controller:
        session = controller.new_session()["sessionId"]
        assert controller.prompt(session, "Synthetic") == {"stopReason": "end_turn"}
        notifications = controller.drain_notifications()
        assert len(notifications) == 1
        assert notifications[0]["update"]["content"]["text"] == "日本語" * fragments
        assert "PRIVATE_SYNTHETIC" not in str(notifications)
    assert record.exists()


def test_private_thoughts_do_not_need_a_queue_slot_when_controls_fill_queue(owned):
    for _ in range(acp.MAX_NOTIFICATIONS):
        handle(owned, {"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "owned", "update": {"sessionUpdate": "usage_update", "used": 100}}})
    handle(owned, notice("agent_thought_chunk", "PRIVATE_SYNTHETIC"))
    assert len(owned.drain_notifications()) == acp.MAX_NOTIFICATIONS


@pytest.mark.parametrize("mode", ["stream-text-1025", "stream-thought-1025", "stream-mixed-513"])
def test_original_fragments_are_bounded_before_coalescing_or_discard(fake, mode):
    make, _, _ = fake
    with make(mode, allow_model_requests=True, _controller_type=TextController) as controller:
        session = controller.new_session()["sessionId"]
        with pytest.raises(acp.ACPError) as error:
            controller.prompt(session, "Synthetic")
        assert error.value.code == "limit_exceeded"
        assert controller._process.returncode is not None


def test_utf8_byte_limits_and_64k_coalesced_blocks(owned):
    for _ in range(16):
        handle(owned, notice(text="é" * (acp.MAX_TEXT_FRAGMENT_BYTES // 2)))
    notifications = owned.drain_notifications()
    assert len(notifications) == 16
    assert all(len(item["update"]["content"]["text"].encode()) == 64 * 1024
               for item in notifications)
    assert owned._text_bytes == acp.MAX_TEXT_BYTES
    with pytest.raises(acp.ACPError) as error:
        handle(owned, notice(text="é"))
    assert error.value.code == "limit_exceeded"


def test_private_bytes_share_the_same_bound_without_retention(owned):
    for _ in range(16):
        handle(owned, notice("agent_thought_chunk", "x" * acp.MAX_TEXT_FRAGMENT_BYTES))
    assert owned.drain_notifications() == []
    with pytest.raises(acp.ACPError) as error:
        handle(owned, notice(text="x"))
    assert error.value.code == "limit_exceeded"


@pytest.mark.parametrize("text", ["é" * 32769, "\ud800", "\x00", "\x1b", "\x7f"])
def test_invalid_or_oversized_text_is_never_hidden_by_coalescing(owned, text):
    with pytest.raises(acp.ACPError):
        handle(owned, notice(text=text))
    assert owned.drain_notifications() == []


@pytest.mark.parametrize("change", ["foreign", "params_extra", "update_extra", "content_extra",
                                  "nontext", "not_string", "rpc_request"])
def test_owned_plain_text_only_and_request_handling_are_preserved(owned, monkeypatch, change):
    value = notice()
    if change == "foreign":
        value["params"]["sessionId"] = "foreign"
    elif change == "params_extra":
        value["params"]["unknown"] = "must reject"
    elif change == "update_extra":
        value["params"]["update"]["unknown"] = "must reject"
    elif change == "content_extra":
        value["params"]["update"]["content"]["annotations"] = {}
    elif change == "nontext":
        value["params"]["update"]["content"]["type"] = "image"
    elif change == "not_string":
        value["params"]["update"]["content"]["text"] = True
    else:
        value["id"] = "native-request"
        replies = []
        monkeypatch.setattr(owned, "_write", lambda reply, _: replies.append(reply))
        handle(owned, value)
        assert replies[0]["error"]["code"] == -32601
        assert owned.rejected_methods == ["session/update"]
        assert owned.drain_notifications() == []
        return
    with pytest.raises(acp.ACPError):
        handle(owned, value)
    assert owned.drain_notifications() == []


def test_metadata_is_opaque_and_not_retained_but_cannot_change_content_shape(owned):
    value = notice(_meta={"private": "PRIVATE_METADATA"})
    value["params"]["update"]["_meta"] = {"private": "PRIVATE_METADATA"}
    before = copy.deepcopy(value)
    handle(owned, value)
    handle(owned, notice(text=" two"))
    result = owned.drain_notifications()
    assert result == [notice(text="synthetic two")["params"]]
    assert value == before
    assert "PRIVATE_METADATA" not in str(result)


@pytest.mark.parametrize("kind", ["tool_call", "tool_call_update", "future_update"])
def test_control_and_tool_updates_retain_order_and_original_queue_limit(owned, kind):
    handle(owned, notice(text="before"))
    value = {"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": "owned", "update": {"sessionUpdate": kind, "toolCallId": "synthetic"}}}
    handle(owned, value)
    handle(owned, notice(text="after"))
    assert [item["update"]["sessionUpdate"] for item in owned._notifications] == [
        "agent_message_chunk", kind, "agent_message_chunk"]
    for _ in range(acp.MAX_NOTIFICATIONS - 3):
        handle(owned, value)
    with pytest.raises(acp.ACPError) as error:
        handle(owned, value)
    assert error.value.code == "limit_exceeded"


def test_provisional_updates_keep_old_quarantine_and_quota(fake):
    make, _, _ = fake
    controller = make(allow_model_requests=True, _controller_type=TextController)
    controller._pending_method = "session/new"
    for _ in range(acp.MAX_NOTIFICATIONS):
        handle(controller, notice(session="provisional"))
    assert controller.drain_notifications() == []
    assert len(controller._provisional_notifications) == acp.MAX_NOTIFICATIONS
    with pytest.raises(acp.ACPError) as error:
        handle(controller, notice(session="provisional"))
    assert error.value.code == "limit_exceeded"
    controller.close()
    assert controller._provisional_notifications == []
