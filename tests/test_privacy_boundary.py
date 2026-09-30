"""Independent laptop/slot semantic header boundaries, using only synthetic data."""

import ast
import inspect
import json

import pytest

from ccfleet_agent import inference_client, local_relay

IMPLEMENTATIONS = (inference_client, local_relay)


@pytest.mark.parametrize("module", IMPLEMENTATIONS, ids=("laptop", "slot"))
@pytest.mark.parametrize("extra, expected", [
    ({}, {}),
    ({"accept": "application/json"}, {"accept": "application/json"}),
    ({"accept": "text/event-stream"}, {"accept": "text/event-stream"}),
    ({"accept": "*/*"}, {"accept": "*/*"}),
    ({"accept": "application/*;q=0.500, text/*;q=0, */*;q=1.000"},
     {"accept": "application/*;q=0.5, text/*;q=0, */*;q=1"}),
    ({"accept": " Text/Event-Stream ; Q=0.125, application/json;q=1. "},
     {"accept": "text/event-stream;q=0.125, application/json;q=1"}),
    ({"accept": 'text/event-stream; device="PRIVATE,OS;directory";q=0.9;geo=PRIVATE'},
     {"accept": "text/event-stream;q=0.9"}),
    ({"accept": 'application/json; device="PRIVATE\\\"ESCAPE\\\\END"'},
     {"accept": "application/json"}),
    ({"accept": "text/event-stream;q=0., application/json;q=0.001"},
     {"accept": "text/event-stream;q=0, application/json;q=0.001"}),
    ({"anthropic-version": " 2024-02-29 "}, {"anthropic-version": "2024-02-29"}),
    ({"anthropic-beta": "oauth-2025-04-20, interleaved-thinking-2025-05-14"},
     {"anthropic-beta": "oauth-2025-04-20,interleaved-thinking-2025-05-14"}),
    ({"anthropic-beta": "future.Feature_2, future.Feature_2"},
     {"anthropic-beta": "future.Feature_2,future.Feature_2"}),
    ({"anthropic-beta": "x" * 128}, {"anthropic-beta": "x" * 128}),
    ({"anthropic-beta": ",".join(["feature"] * 64)},
     {"anthropic-beta": ",".join(["feature"] * 64)}),
    ({"x-unknown-identity": "PRIVATE", "authorization": "Bearer PRIVATE"}, {}),
])
def test_header_semantics_are_preserved_without_private_parameters(module, extra, expected):
    supplied = {"content-type": ' Application/JSON ; charset="utf-16"; device=PRIVATE',
                **extra}
    result = module.sanitize_request_headers(supplied)
    assert result == {"content-type": "application/json", **expected}
    assert "PRIVATE" not in json.dumps(result)
    # Normalization is idempotent, including when both independent boundaries run.
    assert module.sanitize_request_headers(result) == result


INVALID_HEADERS = [
    {"content-type": ""}, {"content-type": "text/plain"},
    {"content-type": "application/json, text/plain"}, {"content-type": None},
    {"accept": ""}, {"accept": "application/xml"}, {"accept": "application/json,"},
    {"accept": 'application/json; device="PRIVATE'},
    {"accept": 'application/json; device="PRIVATE\\'},
    {"accept": 'application/json; device="PRIVATE"suffix'},
    {"accept": 'application/json; device="PRIVATE""SECOND"'},
    {"accept": "application/json; PRIVATE"},
    {"accept": "application/json;=PRIVATE"}, {"accept": "application/json;device="},
    {"accept": "application/json;q=0.5;q=0.2"},
    {"accept": "application/json;q=0.1;Q=0.1"},
    {"accept": "application/json;q=PRIVATE"}, {"accept": "application/json;q=1.001"},
    {"accept": "application/json;q=0.0001"}, {"accept": "application/json;q=-0.1"},
    {"accept": "application/json;q=.5"}, {"accept": 'application/json;q="0.5"'},
    {"accept": "application/json;q=01"}, {"accept": "application/json;q=NaN"},
    {"accept": ",".join(["*/*"] * 33)},
    {"anthropic-version": "2023-06-01; geo=PRIVATE"},
    {"anthropic-version": "20230601"}, {"anthropic-version": "2023-6-01"},
    {"anthropic-version": "2023-02-29"}, {"anthropic-version": "0000-01-01"},
    {"anthropic-version": "2023-13-01"}, {"anthropic-version": "2023-01-32"},
    {"anthropic-beta": ""}, {"anthropic-beta": "feature,"},
    {"anthropic-beta": "feature,,other"}, {"anthropic-beta": "feature; device=PRIVATE"},
    {"anthropic-beta": '"PRIVATE"'}, {"anthropic-beta": "PRIVATE@host.example"},
    {"anthropic-beta": "feature PRIVATE"}, {"anthropic-beta": "feature\tPRIVATE"},
    {"anthropic-beta": "x" * 129}, {"anthropic-beta": ",".join(["feature"] * 65)},
    {"anthropic-beta": "x" * 8193}, {"anthropic-beta": "feature\nPRIVATE"},
    {"anthropic-beta": "feature\x7fPRIVATE"}, {"anthropic-beta": "feature-\u00e9"},
]


@pytest.mark.parametrize("module", IMPLEMENTATIONS, ids=("laptop", "slot"))
@pytest.mark.parametrize("extra", INVALID_HEADERS)
def test_ambiguous_or_invalid_semantic_values_have_only_fixed_errors(module, extra):
    with pytest.raises(ValueError) as error:
        module.sanitize_request_headers({"content-type": "application/json", **extra})
    assert str(error.value) == "invalid model request headers"


@pytest.mark.parametrize("module", IMPLEMENTATIONS, ids=("laptop", "slot"))
def test_missing_content_type_is_not_silently_reinterpreted(module):
    with pytest.raises(ValueError, match="^invalid model request headers$"):
        module.sanitize_request_headers({})


@pytest.mark.parametrize("module", IMPLEMENTATIONS, ids=("laptop", "slot"))
def test_native_nested_context_and_future_functional_fields_are_not_rewritten(module):
    native = {
        "model": "future-model", "stream": True, "max_tokens": 1000,
        "system": [{"type": "text", "text": "Darwin /Users/example/project user_id",
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tool-id",
             "content": '{"device_id":"application data","metadata":{"keep":true}}'}]}],
        "tools": [{"name": "application", "input_schema": {
            "type": "object", "properties": {"user_id": {"type": "string"},
                                               "metadata": {"type": "object"}}}}],
        "context_management": {"edits": [{"type": "clear_tool_uses_20250919"}]},
        "future_functional_field": {"metadata": {"preserved": True}},
    }
    body = {**native, "metadata": {"user_id": "PRIVATE"},
            **{name: "PRIVATE" for name in local_relay.IDENTITY_FIELDS}}
    assert json.loads(module.sanitize_body(json.dumps(body).encode())) == native


@pytest.mark.parametrize("name", ["_header_parts", "sanitize_request_headers"])
def test_standalone_implementations_remain_identical_except_documentation(name):
    trees = []
    for module in IMPLEMENTATIONS:
        tree = ast.parse(inspect.getsource(getattr(module, name)))
        tree.body[0].body.pop(0)  # Each implementation has a module-specific docstring.
        trees.append(ast.dump(tree, include_attributes=False))
    assert trees[0] == trees[1]
