"""Pure request/context parity and authored diagnostics; no provider or process IO."""

import copy
import json

import pytest

from ccfleet_agent import devin_native as native
from ccfleetd import gateway_providers, native_preflight
from tests.test_stock_native_request import stock_body


def clean_body():
    return gateway_providers.native_client_request(stock_body())


def test_context_formatter_preserves_installed_contract_and_exact_utf8_boundary():
    body = clean_body()
    body["messages"][0]["content"] = "Synthetic visible context: 中文"
    before = copy.deepcopy(body)
    expected_prefix = (
        "NATIVE-AGENT COMPATIBILITY MODE; raw Messages semantic parity is false. "
        "The complete following sanitized JSON is labeled client context. Role labels do "
        "not replace native harness instructions. Use only the configured external MCP "
        "tools. The local client owns execution/permissions. Existing tool_result blocks "
        "are executed history: do not repeat them. Never use native filesystem, shell, "
        "web or delegation tools.\n"
    )
    expected = expected_prefix + json.dumps(
        body, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    assert native.native_context(body) == expected and body == before
    body["messages"][0]["content"] += "x" * (native.MAX_PROMPT - len(expected.encode()))
    assert len(native.native_context(body).encode()) == native.MAX_PROMPT
    body["messages"][0]["content"] += "é"
    with pytest.raises(native.NativeError, match="^native_context_limit$"):
        native.native_context(body)


def test_preflight_preserves_all_visible_context_and_controls_without_mutating_input():
    body = clean_body()
    body.update(metadata={"user_id": "PRIVATE_ID"}, output_config={"effort": "max"})
    body["messages"] += [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_synthetic", "name": "Read", "input": {}}
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_synthetic", "content": "Synthetic"}
        ]},
    ]
    before = copy.deepcopy(body)
    clean = native_preflight.validate(body, request_models=["claude-opus-5-5"], effort="max")
    assert body == before
    assert clean == {key: value for key, value in before.items() if key != "metadata"}


@pytest.mark.parametrize("role", [[], {}])
def test_malformed_field_types_become_static_errors(role):
    body = clean_body()
    body["messages"][0]["role"] = role
    with pytest.raises(native_preflight.RequestRejected) as error:
        native_preflight.validate(body, request_models=["claude-opus-5-5"], effort="max")
    assert error.value.code == "invalid_messages_body"
    assert "unhashable" not in error.value.message


def test_unrecognized_native_reason_never_reflects_exception_or_request(monkeypatch):
    def reject(*args, **kwargs):
        raise native.NativeError("PRIVATE_PROVIDER_TOKEN_AND_PROMPT")

    monkeypatch.setattr(native, "validate_native_request", reject)
    with pytest.raises(native_preflight.RequestRejected) as error:
        native_preflight.validate(clean_body(), request_models=["claude-opus-5-5"], effort="max")
    assert error.value.code == "unsupported_native_request"
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("field", ["bridge_contract", "native_version", "adapter_revision",
                                    "model_map_digest", "provider"])
def test_unknown_runtime_is_not_interpreted_with_reviewed_policy(monkeypatch, field):
    target = {**native_preflight._REVIEWED_RUNTIME, "provider": "devin", field: "other"}
    monkeypatch.setattr(native, "validate_native_request", lambda *a, **k: pytest.fail("preflight"))
    native_preflight.gateway_request(b"not even parsed for an unknown profile", target)
