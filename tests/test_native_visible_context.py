"""Bounded assistant-private projection; every visible history value is retained."""

import copy

import pytest

from ccfleet_agent import devin_native as native
from ccfleetd import native_preflight
from ccfleetd.native_visible_context import OMISSION_NOTE, visible_history
from tests.test_native_preflight import clean_body


@pytest.mark.parametrize("private", [
    {"type": "thinking", "thinking": "PRIVATE_REASONING", "signature": "PRIVATE_SIGNATURE"},
    {"type": "redacted_thinking", "data": "PRIVATE_REDACTED"},
    {"type": "thinking", "thinking": "PRIVATE_REASONING", "signature": "PRIVATE_SIGNATURE",
     "cache_control": {"type": "ephemeral", "ttl": "1h"}},
    {"type": "redacted_thinking", "data": "PRIVATE_REDACTED",
     "cache_control": {"type": "ephemeral"}},
])
def test_projection_keeps_visible_blocks_object_identity_order_and_tool_correlation(private):
    body = clean_body()
    visible = [
        {"type": "text", "text": "Visible before"},
        {"type": "tool_use", "id": "toolu_read", "name": "Read", "input": {"path": "visible"}},
        {"type": "text", "text": "Visible after", "cache_control": {"type": "ephemeral"}},
    ]
    assistant = {"role": "assistant", "content": [visible[0], private, *visible[1:]]}
    result = {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_read", "content": "Visible local result"}
    ]}
    body["messages"] += [assistant, result]
    before = copy.deepcopy(body)
    projected = visible_history(body)
    assert body == before and projected["messages"][-1] is result
    assert projected["messages"][-2]["content"] == visible
    assert all(a is b for a, b in zip(projected["messages"][-2]["content"], visible))
    assert projected["tools"] is body["tools"] and projected["system"] is body["system"]
    assert visible_history(projected) is projected
    clean = native_preflight.validate(body, request_models=["claude-opus-5-5"], effort="max")
    assert clean["messages"] == projected["messages"]
    assert b"PRIVATE" not in native._encode(clean)


def test_private_only_assistant_retains_turn_position_with_authored_bridge_note():
    body = clean_body()
    body["messages"] += [
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "PRIVATE", "signature": "PRIVATE"},
            {"type": "redacted_thinking", "data": "PRIVATE"},
        ]},
        {"role": "user", "content": "Continue visible task"},
    ]
    projected = visible_history(body)
    assert projected["messages"][-2] == {
        "role": "assistant", "content": [{"type": "text", "text": OMISSION_NOTE}]
    }
    assert projected["messages"][-1] is body["messages"][-1]


@pytest.mark.parametrize("block", [
    {"type": "thinking", "thinking": "PRIVATE"},
    {"type": "thinking", "thinking": [], "signature": "PRIVATE"},
    {"type": "thinking", "thinking": "PRIVATE", "signature": "PRIVATE", "unknown": True},
    {"type": "redacted_thinking", "data": {}},
    {"type": "redacted_thinking", "data": "PRIVATE", "unknown": "PRIVATE"},
    {"type": "redacted_thinking", "data": "PRIVATE", "cache_control": {"type": "unknown"}},
])
def test_unknown_or_malformed_private_shape_remains_rejected(block):
    body = clean_body()
    body["messages"].append({"role": "assistant", "content": [block]})
    assert visible_history(body) is body
    with pytest.raises(native_preflight.RequestRejected) as error:
        native_preflight.validate(body, request_models=["claude-opus-5-5"], effort="max")
    assert error.value.code == "unsupported_content" and "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("location", ["user", "system", "tool_result"])
def test_reasoning_outside_top_level_assistant_is_not_removed(location):
    body = clean_body()
    block = {"type": "thinking", "thinking": "PRIVATE", "signature": "PRIVATE"}
    if location == "system":
        body["system"] = [block]
    elif location == "tool_result":
        body["messages"].append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_read", "content": [block]}
        ]})
    else:
        body["messages"].append({"role": "user", "content": [block]})
    assert visible_history(body) is body
    with pytest.raises(native_preflight.RequestRejected, match="reasoning shapes"):
        native_preflight.validate(body, request_models=["claude-opus-5-5"], effort="max")


def test_original_resource_bounds_apply_before_private_content_can_shrink():
    body = clean_body()
    body["messages"].append({"role": "assistant", "content": [
        {"type": "thinking", "thinking": "PRIVATE" * (native.MAX_BODY // 7 + 1),
         "signature": "PRIVATE"}
    ]})
    with pytest.raises(native_preflight.RequestRejected) as error:
        native_preflight.validate(body, request_models=["claude-opus-5-5"], effort="max")
    assert error.value.code == "invalid_messages_body" and "PRIVATE" not in str(error.value)


def test_original_node_count_bound_applies_before_private_blocks_can_shrink():
    body = clean_body()
    body["messages"].append({"role": "assistant", "content": [
        {"type": "thinking", "thinking": "Synthetic", "signature": "Synthetic"}
        for _ in range(8000)
    ]})
    with pytest.raises(native_preflight.RequestRejected) as error:
        native_preflight.validate(body, request_models=["claude-opus-5-5"], effort="max")
    assert error.value.code == "invalid_messages_body"
