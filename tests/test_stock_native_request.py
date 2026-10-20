"""Stock CLI metadata compatibility through the unchanged native validator; no IO."""

import copy

import pytest

from ccfleet_agent import devin_native
from ccfleetd import gateway_providers as providers


def stock_body():
    return {
        "model": "claude-opus-5-5",
        "stream": True,
        "output_config": {"effort": "max"},
        "system": [{"type": "text", "text": "Preserve visible system context"}],
        "messages": [{"role": "user", "content": "Preserve visible task"}],
        "tools": [
            {
                "name": "DeferredToolPlaceholder",
                "description": "Reserved placeholder that keeps deferred tool loading active; never call this tool.",
                "input_schema": {"type": "object", "properties": {}},
                "defer_loading": True,
            },
            {"name": "Read", "description": "Local read", "input_schema": {"type": "object"}},
        ],
    }


def validate(value):
    return devin_native.validate_native_request(
        value, request_models=["claude-opus-5-5"], effort="max"
    )


def test_exact_inert_annotation_removed_without_mutation_or_other_context_changes():
    original = stock_body()
    before = copy.deepcopy(original)
    with pytest.raises(devin_native.NativeError, match="unsupported_tool_declaration"):
        validate(original)
    canonical = providers.native_client_request(original)
    assert original == before
    expected = copy.deepcopy(original)
    del expected["tools"][0]["defer_loading"]
    assert canonical == expected and validate(canonical) == expected
    assert canonical["messages"] is original["messages"]
    assert providers.native_client_request(canonical) is canonical


@pytest.mark.parametrize(
    "change", ["false", "number", "string", "name", "description", "schema", "extra", "real_tool"]
)
def test_other_annotation_values_and_declarations_remain_rejected(change):
    body = stock_body()
    tool = body["tools"][0]
    if change == "false":
        tool["defer_loading"] = False
    elif change == "number":
        tool["defer_loading"] = 1
    elif change == "string":
        tool["defer_loading"] = "true"
    elif change == "name":
        tool["name"] = "OtherTool"
    elif change == "description":
        tool["description"] = "Perform an action"
    elif change == "schema":
        tool["input_schema"]["properties"] = {"command": {"type": "string"}}
    elif change == "extra":
        tool["execute"] = True
    else:
        del tool["defer_loading"]
        body["tools"][1]["defer_loading"] = True
    assert providers.native_client_request(body) is body
    with pytest.raises(devin_native.NativeError):
        validate(body)


@pytest.mark.parametrize("change", ["model", "effort", "unknown_control", "thinking", "cache"])
def test_compatibility_cannot_bypass_existing_native_generation_controls(change):
    body = stock_body()
    if change == "model":
        body["model"] = "different"
    elif change == "effort":
        body["output_config"] = {"effort": "low"}
    elif change == "unknown_control":
        body["temperature"] = 1
    elif change == "thinking":
        body["thinking"] = {"type": "enabled", "budget_tokens": 5}
    else:
        body["tools"][0]["cache_control"] = {"type": "unknown"}
    with pytest.raises(devin_native.NativeError):
        validate(providers.native_client_request(body))


def test_tool_history_retained_and_validated_on_continuation():
    body = stock_body()
    body["messages"] += [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_read1234",
                    "name": "Read",
                    "input": {"file_path": "/synthetic/marker"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_read1234",
                    "content": "Synthetic local result",
                }
            ],
        },
    ]
    canonical = providers.native_client_request(body)
    assert validate(canonical)["messages"] == body["messages"]
