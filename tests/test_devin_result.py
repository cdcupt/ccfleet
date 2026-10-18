"""Pure native ACP result fixtures only; no process, account, provider or money calls."""

from __future__ import annotations

import pytest

from ccfleet_agent import devin_result as results

SESSION = "session_owned"
MODEL = "claude-opus-5-5-max"


def update(kind="agent_message_chunk", *, session=SESSION, **fields):
    return {"sessionId": session, "update": {"sessionUpdate": kind, **fields}}


def text(value, **changes):
    return update(content={"type": "text", "text": value}, **changes)


def collect(notifications=(), prompt_result=None, **changes):
    options = {"session_id": SESSION, "configured_model": MODEL}
    options.update(changes)
    return results.validate_result(notifications,
                                   {"stopReason": "end_turn"} if prompt_result is None else prompt_result,
                                   **options)


def test_multilingual_fragments_preserve_exact_text_and_unknown_accounting():
    result = collect([text("日"), text("本語\n"), text("PONG")])
    assert result.text == "日本語\nPONG"
    assert result.stop_reason == "end_turn"
    assert result.configured_model == MODEL
    assert result.served_model is None
    assert result.usage is None
    with pytest.raises(AttributeError):
        result.text = "changed"


def test_context_occupancy_cost_and_model_metadata_never_become_usage_or_identity():
    notices = [update("usage_update", used=1234, size=200000,
                      cost={"amount": 0.01, "currency": "USD"}),
               update("config_option_update", configOptions=[{
                   "category": "model", "currentValue": "claude-opus-5-5-medium"}, {
                   "category": "thought_level", "currentValue": "max"}]),
               update("agent_thought_chunk", content={"type": "text", "text": "private reasoning"}),
               update("user_message_chunk", content={"type": "text", "text": "user context"}),
               text("PONG")]
    result = collect(notices, {"stopReason": "end_turn", "usage": {"input_tokens": 5}})
    assert result.text == "PONG"
    assert result.configured_model == MODEL
    assert result.served_model is None and result.usage is None
    assert not hasattr(result, "cost")


@pytest.mark.parametrize("stop", sorted(results.STOP_REASONS))
def test_recognized_non_success_states_and_empty_text_are_preserved(stop):
    result = collect([], {"stopReason": stop})
    assert result.text == "" and result.stop_reason == stop
    assert not hasattr(result, "success")


@pytest.mark.parametrize("value", [{}, {"stopReason": "future"}, {"stopReason": None},
                                   {"stopReason": []}, {"stopReason": True}, []])
def test_missing_or_unknown_completion_reason_rejects(value):
    with pytest.raises(results.ResultError):
        collect([], value)


@pytest.mark.parametrize("kind", ["tool_call", "tool_call_update"])
def test_tool_status_cannot_be_converted_into_text_success_or_tool_execution(kind):
    with pytest.raises(results.ResultError) as error:
        collect([text("Looks finished"), update(kind, status="completed", toolCallId="x")])
    assert error.value.code == "tool_activity"


@pytest.mark.parametrize("notice", [
    text("spoof", session="session_other"),
    update("usage_update", session="session_other", used=100),
    {"sessionId": SESSION, "update": None},
    {"sessionId": SESSION, "update": {"sessionUpdate": None}},
    {"jsonrpc": "2.0", "id": 1, "method": "fs/read_text_file", "params": {"path": "secret"}},
    update("future_tool_action", payload={}),
    update("agent_message_chunk", content={"type": "image", "data": "pixels"}),
    update("agent_message_chunk", content={"type": "text", "text": "x", "annotations": {}}),
    update("agent_message_chunk", content={"type": "text", "text": 1}),
    text("\ud800"), text("\x00"), text("\x1b"),
])
def test_malformed_spoofed_or_unsupported_updates_fail_closed(notice):
    with pytest.raises(results.ResultError):
        collect([notice])


@pytest.mark.parametrize("notifications", [None, "text", b"bytes", {}])
def test_notification_container_must_be_an_iterable_of_parameters(notifications):
    with pytest.raises(results.ResultError):
        collect(notifications)


@pytest.mark.parametrize("changes", [{"session_id": ""}, {"session_id": []},
                                    {"configured_model": "--model"}, {"configured_model": None},
                                    {"configured_model": "model\nheader"}])
def test_owned_session_and_configured_model_are_required(changes):
    with pytest.raises(results.ResultError) as error:
        collect([], **changes)
    assert error.value.code == "invalid_config"


def test_fragment_byte_limit_counts_utf8_bytes(monkeypatch):
    monkeypatch.setattr(results, "MAX_FRAGMENT_BYTES", 3)
    assert collect([text("日")]).text == "日"
    with pytest.raises(results.ResultError) as error:
        collect([text("日本")])
    assert error.value.code == "limit_exceeded"


def test_total_text_and_fragment_counts_are_bounded(monkeypatch):
    monkeypatch.setattr(results, "MAX_TEXT_BYTES", 4)
    with pytest.raises(results.ResultError):
        collect([text("abc"), text("de")])
    monkeypatch.setattr(results, "MAX_FRAGMENTS", 1)
    with pytest.raises(results.ResultError):
        collect([text(""), text("")])


def test_ignored_metadata_and_generators_still_obey_notification_bounds(monkeypatch):
    monkeypatch.setattr(results, "MAX_NOTIFICATIONS", 2)
    with pytest.raises(results.ResultError) as error:
        collect(update("usage_update", used=i) for i in range(3))
    assert error.value.code == "limit_exceeded"


def test_ignored_metadata_is_bounded_without_retaining_it(monkeypatch):
    monkeypatch.setattr(results, "MAX_METADATA_STRING_BYTES", 200)
    with pytest.raises(results.ResultError) as error:
        collect([update("usage_update", extension="x" * 201)])
    assert error.value.code == "limit_exceeded"


def test_recursive_or_nonfinite_metadata_fails_instead_of_becoming_accounting():
    cyclic = {}
    cyclic["self"] = cyclic
    for value in (cyclic, {"used": float("nan")}, {"used": 1 << 4000}):
        with pytest.raises(results.ResultError):
            collect([update("usage_update", extension=value)])
