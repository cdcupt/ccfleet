"""Pure read handoff tests; no file/tool/process/provider execution."""

from __future__ import annotations

import copy
import threading
import time

import pytest

from ccfleet_agent import devin_read_bridge as bridge

BINDING = "server-owner:key:generation-7"
SESSION = "session_owned"
CONTEXT = {"binding": BINDING, "session_id": SESSION, "request_id": "native-1"}


def request(**changes):
    value = {"jsonrpc": "2.0", "id": "native-1", "method": "fs/read_text_file",
             "params": {"sessionId": SESSION, "path": "/client/repo/日本語.py", "line": 2, "limit": 10}}
    value.update(changes)
    return value


def pending(**options):
    handoff = bridge.ReadHandoff(binding=BINDING, session_id=SESSION, **options)
    tool = handoff.begin(request(), remaining_seconds=2)
    return handoff, tool


def result(tool, **changes):
    value = {"type": "tool_result", "tool_use_id": tool.tool_id, "content": "file 日本語\n"}
    value.update(changes)
    return value


def test_correlated_read_is_suspended_then_resolved_exactly_once():
    handoff, tool = pending()
    assert tool.as_tool_use() == {"type": "tool_use", "id": tool.tool_id, "name": "Read",
                                 "input": {"file_path": "/client/repo/日本語.py", "offset": 2, "limit": 10}}
    assert BINDING not in repr(tool)
    assert tool.tool_id != "native-1"
    assert handoff.pending(**CONTEXT) == tool
    received, started = [], threading.Event()

    def waiter():
        started.set()
        received.append(handoff.wait())

    thread = threading.Thread(target=waiter)
    thread.start()
    assert started.wait(1)
    handoff.resolve(result(tool), **CONTEXT)
    thread.join(1)
    assert not thread.is_alive()
    assert received == [{"jsonrpc": "2.0", "id": "native-1", "result": {"content": "file 日本語\n"}}]
    for operation in (handoff.wait, lambda: handoff.resolve(result(tool), **CONTEXT),
                      lambda: handoff.begin(request(), remaining_seconds=1)):
        with pytest.raises(bridge.ReadBridgeError) as error:
            operation()
        assert error.value.code == "replay"


def test_correlation_ids_are_random_and_returned_tool_dict_mutation_is_isolated():
    one, first = pending()
    two, second = pending()
    assert first.tool_id != second.tool_id
    body = first.as_tool_use()
    body["input"]["file_path"] = "/changed"
    assert one.pending(**CONTEXT).path == "/client/repo/日本語.py"
    one.cancel(**CONTEXT)
    two.cancel(**CONTEXT)


@pytest.mark.parametrize("changes,code", [
    ({"binding": "other-key-generation"}, "binding_mismatch"),
    ({"session_id": "other_session"}, "session_mismatch"),
    ({"request_id": "other-native-id"}, "request_mismatch"),
    ({"request_id": 1}, "request_mismatch"),
])
def test_mismatched_server_context_cannot_resolve_or_cancel_a_read(changes, code):
    handoff, tool = pending()
    context = dict(CONTEXT, **changes)
    for operation in (lambda: handoff.resolve(result(tool), **context),
                      lambda: handoff.pending(**context), lambda: handoff.cancel(**context)):
        with pytest.raises(bridge.ReadBridgeError) as error:
            operation()
        assert error.value.code == code
    handoff.resolve(result(tool), **CONTEXT)
    assert handoff.wait()["result"]["content"] == "file 日本語\n"


def test_wrong_tool_id_does_not_consume_the_real_result():
    handoff, tool = pending()
    with pytest.raises(bridge.ReadBridgeError) as error:
        handoff.resolve(result(tool, tool_use_id="toolu_wrong"), **CONTEXT)
    assert error.value.code == "tool_mismatch"
    handoff.resolve(result(tool), **CONTEXT)
    assert handoff.wait()["id"] == "native-1"


def test_cancel_wakes_waiter_and_late_results_cannot_revive_handoff():
    handoff, tool = pending()
    handoff.cancel(**CONTEXT)
    for operation in (handoff.wait, lambda: handoff.resolve(result(tool), **CONTEXT)):
        with pytest.raises(bridge.ReadBridgeError) as error:
            operation()
        assert error.value.code == "cancelled"


def test_revocation_after_resolve_discards_ready_response_before_delivery():
    handoff, tool = pending()
    handoff.resolve(result(tool), **CONTEXT)
    handoff.cancel(**CONTEXT)
    with pytest.raises(bridge.ReadBridgeError) as error:
        handoff.wait()
    assert error.value.code == "cancelled"
    assert handoff._response is None
    with pytest.raises(bridge.ReadBridgeError) as error:
        handoff.resolve(result(tool), **CONTEXT)
    assert error.value.code == "cancelled"


def test_deadline_expires_without_retries_or_late_result_acceptance(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock[0])
    handoff, tool = pending()
    clock[0] += 3
    for operation in (handoff.wait, lambda: handoff.resolve(result(tool), **CONTEXT)):
        with pytest.raises(bridge.ReadBridgeError) as error:
            operation()
        assert error.value.code == "timeout"


def test_frontend_error_is_an_rpc_error_not_fake_file_contents():
    handoff, tool = pending()
    handoff.resolve(result(tool, is_error=True, content="private path and error details"), **CONTEXT)
    assert handoff.wait() == {"jsonrpc": "2.0", "id": "native-1", "error": {
        "code": -32000, "message": "Frontend read failed"}}


def test_plain_text_fragments_preserve_text_without_invented_read_normalization():
    handoff, tool = pending()
    handoff.resolve(result(tool, content=[{"type": "text", "text": "2→line\n"},
                                          {"type": "text", "text": "3→next"}]), **CONTEXT)
    assert handoff.wait()["result"] == {"content": "2→line\n3→next"}


@pytest.mark.parametrize("value", [
    {"method": "session/update", "params": {"sessionUpdate": "tool_call"}},
    {"jsonrpc": "2.0", "id": "x", "method": "terminal/create", "params": {}},
    request(id=True), request(id=None), request(id=[]),
    request(params={"sessionId": "other", "path": "/client/task"}),
    request(params={"sessionId": SESSION, "path": "relative"}),
    request(params={"sessionId": SESSION, "path": "/client/../secret"}),
    request(params={"sessionId": SESSION, "path": "//network/share"}),
    request(params={"sessionId": SESSION, "path": "/client\nheader"}),
    request(params={"sessionId": SESSION, "path": "/client", "line": True}),
    request(params={"sessionId": SESSION, "path": "/client", "limit": 0}),
    request(params={"sessionId": SESSION, "path": "/client", "limit": 10001}),
    request(params={"sessionId": SESSION, "path": "/client", "unknown": "x"}),
])
def test_only_valid_native_read_requests_can_begin_handoff(value):
    handoff = bridge.ReadHandoff(binding=BINDING, session_id=SESSION)
    with pytest.raises(bridge.ReadBridgeError):
        handoff.begin(copy.deepcopy(value), remaining_seconds=1)


@pytest.mark.parametrize("content", [None, {}, [{"type": "image", "data": "x"}],
                                     [{"type": "text", "text": 1}], "\ud800", "\x00"])
def test_malformed_results_do_not_release_the_waiting_native_rpc(content):
    handoff, tool = pending()
    with pytest.raises(bridge.ReadBridgeError):
        handoff.resolve(result(tool, content=content), **CONTEXT)
    assert handoff.pending(**CONTEXT) == tool
    handoff.cancel(**CONTEXT)


def test_result_bounds_count_utf8_bytes_and_all_fragments():
    handoff, tool = pending(max_result_bytes=4)
    with pytest.raises(bridge.ReadBridgeError) as error:
        handoff.resolve(result(tool, content="日本"), **CONTEXT)
    assert error.value.code == "limit_exceeded"
    with pytest.raises(bridge.ReadBridgeError):
        handoff.resolve(result(tool, content=[{"type": "text", "text": "abc"},
                                              {"type": "text", "text": "de"}]), **CONTEXT)
    handoff.resolve(result(tool, content="日"), **CONTEXT)
    assert handoff.wait()["result"]["content"] == "日"


@pytest.mark.parametrize("seconds", [0, -1, 61, True, float("nan"), float("inf")])
def test_remaining_deadline_must_be_explicit_and_bounded(seconds):
    handoff = bridge.ReadHandoff(binding=BINDING, session_id=SESSION)
    with pytest.raises(bridge.ReadBridgeError):
        handoff.begin(request(), remaining_seconds=seconds)


def test_numeric_native_rpc_id_preserves_type_and_wait_requires_begin():
    handoff = bridge.ReadHandoff(binding=BINDING, session_id=SESSION)
    with pytest.raises(bridge.ReadBridgeError):
        handoff.wait()
    tool = handoff.begin(request(id=7), remaining_seconds=1)
    with pytest.raises(bridge.ReadBridgeError):
        handoff.resolve(result(tool), **dict(CONTEXT, request_id="7"))
    handoff.resolve(result(tool), **dict(CONTEXT, request_id=7))
    assert type(handoff.wait()["id"]) is int


def test_wait_deadline_is_bounded_without_a_frontend_result():
    handoff = bridge.ReadHandoff(binding=BINDING, session_id=SESSION)
    handoff.begin(request(), remaining_seconds=0.02)
    started = time.monotonic()
    with pytest.raises(bridge.ReadBridgeError) as error:
        handoff.wait()
    assert error.value.code == "timeout"
    assert time.monotonic() - started < 1


def test_resolution_that_finishes_validation_after_deadline_is_rejected(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock[0])
    handoff, tool = pending()
    original = bridge._text_bytes

    def delayed(value, maximum, name):
        size = original(value, maximum, name)
        clock[0] += 3
        return size

    monkeypatch.setattr(bridge, "_text_bytes", delayed)
    with pytest.raises(bridge.ReadBridgeError) as error:
        handoff.resolve(result(tool), **CONTEXT)
    assert error.value.code == "timeout"


def test_expired_response_is_not_delivered_late(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock[0])
    handoff, tool = pending()
    handoff.resolve(result(tool), **CONTEXT)
    clock[0] += 3
    with pytest.raises(bridge.ReadBridgeError) as error:
        handoff.wait()
    assert error.value.code == "timeout"


def test_excess_result_fragments_are_rejected_before_delivery():
    handoff, tool = pending()
    with pytest.raises(bridge.ReadBridgeError) as error:
        handoff.resolve(result(tool, content=[{"type": "text", "text": ""}]
                                * (bridge.MAX_RESULT_FRAGMENTS + 1)), **CONTEXT)
    assert error.value.code == "limit_exceeded"
    handoff.cancel(**CONTEXT)
