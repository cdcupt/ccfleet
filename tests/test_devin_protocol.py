"""Native caller-side tool loops and byte-split vendor fixtures; no model/network calls."""

from __future__ import annotations

import copy
import json

import pytest

from ccfleet_agent import devin_protocol as protocol

MODEL_MAP = {"claude-sonnet": "claude-sonnet-verified-medium"}
UID = MODEL_MAP["claude-sonnet"]
TOOLS = [
    {"name": "Read", "description": "Read a file", "input_schema": {
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "Bash", "input_schema": {
        "type": "object", "properties": {"command": {"type": "string"}}}},
]


def payload(**changes):
    value = {"model": "claude-sonnet", "max_tokens": 1024,
             "messages": [{"role": "user", "content": "Find the bug 日本語"}], "tools": TOOLS}
    value.update(changes)
    return copy.deepcopy(value)


def prepared(**changes):
    return protocol.prepare_request(payload(**changes), model_map=MODEL_MAP)


def completed(content="Done", calls=None, finish="stop", **changes):
    value = {"id": "chatcmpl_verified", "object": "chat.completion", "model": UID,
             "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                          "finish_reason": finish}],
             "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}
    if calls is not None:
        value["choices"][0]["message"]["tool_calls"] = calls
    value.update(changes)
    return value


def call(tool_id="call_read", name="Read", arguments='{"path":"a.py"}'):
    return {"id": tool_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def chunk(delta=None, finish=None, usage=None, *, choices=True, **changes):
    value = {"id": "chatcmpl_verified", "object": "chat.completion.chunk", "model": UID,
             "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]
             if choices else []}
    if usage is not None:
        value["usage"] = usage
    value.update(changes)
    return value


def sse(events, *, done=True, crlf=False):
    separator = b"\r\n\r\n" if crlf else b"\n\n"
    data = separator.join(b"data: " + json.dumps(item, ensure_ascii=False).encode("utf-8")
                          for item in events) + separator
    return data + (b"data: [DONE]" + separator if done else b"")


def split_bytes(data, size=7):
    return [data[index:index + size] for index in range(0, len(data), size)]


def events_from(data):
    result = []
    for item in b"".join(data).split(b"\n\n"):
        if item:
            name, raw = item.split(b"\n", 1)
            value = json.loads(raw.removeprefix(b"data: "))
            assert name == b"event: " + value["type"].encode()
            result.append(value)
    return result


def usage():
    return {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13,
            "prompt_tokens_details": {"cached_tokens": 2}}


def assert_no_emission(data, *, request=None, code=None):
    output = protocol.translate_sse(data, request or prepared(stream=True))
    with pytest.raises(protocol.DevinProtocolError) as error:
        next(output)
    if code is not None:
        assert error.value.code == code


def test_text_system_and_numeric_controls_use_only_explicit_verified_uid():
    source = payload(system=[{"type": "text", "text": "Keep "}, {"type": "text", "text": "tools local."}],
                     stream=True, temperature=0.2, top_p=0.8,
                     tool_choice={"type": "tool", "name": "Read", "disable_parallel_tool_use": True})
    request = protocol.prepare_request(source, model_map=MODEL_MAP)
    assert request.requested_model == "claude-sonnet"
    assert request.actual_model == UID
    assert request.body["model"] == UID
    assert request.body["messages"][0] == {"role": "system", "content": "Keep tools local."}
    assert request.body["stream_options"] == {"include_usage": True}
    assert request.body["tool_choice"] == {"type": "function", "function": {"name": "Read"}}
    assert request.body["parallel_tool_calls"] is False
    assert request.body["temperature"] == 0.2
    assert request.body["top_p"] == 0.8
    source["tools"][0]["input_schema"]["properties"].clear()
    assert request.body["tools"][0]["function"]["parameters"]["properties"]["path"] == {"type": "string"}


def test_parallel_native_tool_round_trip_retains_ids_inputs_and_results():
    first = prepared()
    native = protocol.translate_response(completed(
        content="Inspecting.", calls=[call(), call("call_bash", "Bash", '{"command":"pytest"}')],
        finish="tool_calls"), first)
    assert native["content"] == [
        {"type": "text", "text": "Inspecting."},
        {"type": "tool_use", "id": "call_read", "name": "Read", "input": {"path": "a.py"}},
        {"type": "tool_use", "id": "call_bash", "name": "Bash", "input": {"command": "pytest"}},
    ]
    next_request = payload(messages=[
        {"role": "user", "content": "Inspect"},
        {"role": "assistant", "content": native["content"]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_bash", "content": "2 passed", "is_error": False},
            {"type": "tool_result", "tool_use_id": "call_read", "content": [
                {"type": "text", "text": "print("}, {"type": "text", "text": "1)"}]},
            {"type": "text", "text": "Now fix it."},
        ]},
    ])
    result = protocol.prepare_request(next_request, model_map=MODEL_MAP)
    assert result.body["messages"][1]["tool_calls"] == [
        call(), call("call_bash", "Bash", '{"command":"pytest"}')]
    assert result.body["messages"][2:] == [
        {"role": "tool", "tool_call_id": "call_bash", "content": "2 passed"},
        {"role": "tool", "tool_call_id": "call_read", "content": "print(1)"},
        {"role": "user", "content": "Now fix it."},
    ]
    assert result.prior_tool_ids == frozenset({"call_read", "call_bash"})


@pytest.mark.parametrize("change", [
    {"thinking": {"type": "enabled", "budget_tokens": 1000}},
    {"output_config": {"format": {"type": "json_schema"}}},
    {"metadata": {"user_id": "owner"}},
    {"stop_sequences": ["END"]},
    {"system": [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}]},
    {"messages": [{"role": "user", "content": [{"type": "image", "source": {}}]}]},
    {"messages": [{"role": "assistant", "content": [{"type": "thinking", "thinking": "x", "signature": "x"}]}]},
    {"tools": [{"type": "web_search_20250305", "name": "web_search"}]},
    {"tools": [{**TOOLS[0], "cache_control": {"type": "ephemeral"}}]},
    {"tools": [{**TOOLS[0], "strict": True}]},
    {"tool_choice": {"type": "vendor_future_option"}},
])
def test_unsupported_semantics_fail_before_a_request_is_prepared(change):
    with pytest.raises(protocol.DevinProtocolError) as error:
        prepared(**change)
    assert error.value.code == "unsupported_feature"


@pytest.mark.parametrize("change", [
    {"model": "unverified"}, {"model": "claude-sonnet\nother"},
    {"max_tokens": True}, {"max_tokens": 0}, {"max_tokens": protocol.MAX_OUTPUT_TOKENS + 1},
    {"stream": 1}, {"temperature": float("nan")}, {"temperature": 10 ** 1000},
    {"top_p": -0.1}, {"messages": []}, {"messages": [{"role": "system", "content": "x"}]},
    {"messages": [{"role": "user", "content": "bad\x00text"}]},
    {"messages": [{"role": "user", "content": "bad\x1btext"}]},
    {"tools": [TOOLS[0], TOOLS[0]]},
    {"tool_choice": {"type": "tool", "name": "Missing"}},
    {"tool_choice": {"type": "any", "disable_parallel_tool_use": 0}},
    {"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "orphan", "content": "x"}]}]},
])
def test_invalid_types_ids_controls_and_bounds_fail_closed(change):
    with pytest.raises(protocol.DevinProtocolError):
        prepared(**change)


def test_duplicate_json_fields_and_untrusted_mapping_are_rejected():
    with pytest.raises(protocol.DevinProtocolError, match="duplicate"):
        protocol.prepare_request(b'{"model":"claude-sonnet","model":"other"}', model_map=MODEL_MAP)
    with pytest.raises(protocol.DevinProtocolError) as error:
        protocol.prepare_request(payload(), model_map={})
    assert error.value.code == "unmapped_model"
    with pytest.raises(protocol.DevinProtocolError):
        protocol.prepare_request(payload(), model_map={"claude-sonnet": "UID\r\nheader"})
    with pytest.raises(protocol.DevinProtocolError):
        protocol.prepare_request(json.dumps(payload()).encode("utf-16"), model_map=MODEL_MAP)


@pytest.mark.parametrize("mutation", ["missing_result", "duplicate_call", "duplicate_result", "error_result", "text_before_result"])
def test_invalid_tool_histories_do_not_turn_into_new_agent_tasks(mutation):
    calls = [{"type": "tool_use", "id": "call_read", "name": "Read", "input": {"path": "a.py"}}]
    results = [{"type": "tool_result", "tool_use_id": "call_read", "content": "ok"}]
    if mutation == "duplicate_call":
        calls.append(copy.deepcopy(calls[0]))
    elif mutation == "duplicate_result":
        results.append(copy.deepcopy(results[0]))
    elif mutation == "error_result":
        results[0]["is_error"] = True
    elif mutation == "text_before_result":
        results.insert(0, {"type": "text", "text": "interleaved"})
    messages = [{"role": "assistant", "content": calls}]
    if mutation != "missing_result":
        messages.append({"role": "user", "content": results})
    with pytest.raises(protocol.DevinProtocolError):
        prepared(messages=messages)


def test_completed_empty_response_and_cached_usage_keep_actual_counts():
    result = protocol.translate_response(completed("", usage=usage()), prepared())
    assert result["content"] == []
    assert result["model"] == UID
    assert result["stop_reason"] == "end_turn"
    assert result["usage"] == {"input_tokens": 8, "output_tokens": 3, "cache_read_input_tokens": 2}
    assert "cache_creation_input_tokens" not in result["usage"]


@pytest.mark.parametrize("response", [
    completed(model="other-uid"), completed(usage=None), completed(usage={}),
    completed(object="text_completion"), completed(created=True),
    completed(usage={"prompt_tokens": True, "completion_tokens": 1}),
    completed(usage={"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 999}),
    completed(usage={"prompt_tokens": 10, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 11}}),
    completed(calls={}), completed(calls=""),
    completed(calls=[call(arguments='{"x":1,"x":2}')], finish="tool_calls"),
    completed(calls=[call(arguments="[]")], finish="tool_calls"),
    completed(calls=[call(), call()], finish="tool_calls"),
    completed(calls=[call(name="Unknown")], finish="tool_calls"),
    completed(calls=[call()], finish="stop"), completed(finish="content_filter"),
    completed(choices=[]), completed(error={"message": "private upstream error"}),
])
def test_completed_response_rejects_missing_usage_identity_and_tool_errors(response):
    with pytest.raises(protocol.DevinProtocolError):
        protocol.translate_response(response, prepared())


def test_sse_parallel_indexed_argument_fragments_and_multibyte_crlf_boundaries():
    argument_fragments = {0: ['{"path":', '"日本語.py"}'], 1: ['{"command":"', 'pytest"}']}
    stream = [
        chunk({"role": "assistant", "content": "Inspect 日本語."}),
        chunk({"tool_calls": [
            {"index": 0, **call(arguments=argument_fragments[0][0])},
            {"index": 1, **call("call_bash", "Bash", argument_fragments[1][0])},
        ]}),
        chunk({"tool_calls": [
            {"index": 1, "function": {"arguments": argument_fragments[1][1]}},
            {"index": 0, "function": {"arguments": argument_fragments[0][1]}},
        ]}),
        chunk(finish="tool_calls"), chunk(choices=False, usage=usage()),
    ]
    raw = b": keepalive\r\n\r\n" + sse(stream, crlf=True)
    events = events_from(protocol.translate_sse(split_bytes(raw, 1), prepared(stream=True)))
    assert events[0]["type"] == "message_start"
    assert events[0]["message"]["model"] == UID
    assert events[0]["message"]["usage"] == {"input_tokens": 8, "output_tokens": 3, "cache_read_input_tokens": 2}
    starts = [e for e in events if e["type"] == "content_block_start"]
    assert [e["index"] for e in starts] == [0, 1, 2]
    assert starts[1]["content_block"] == {"type": "tool_use", "id": "call_read", "name": "Read", "input": {}}
    assert starts[2]["content_block"]["id"] == "call_bash"
    assert [e["delta"]["text"] for e in events if e.get("delta", {}).get("type") == "text_delta"] == ["Inspect 日本語."]
    for upstream_index in (0, 1):
        fragments = [e["delta"]["partial_json"] for e in events
                     if e["type"] == "content_block_delta" and e["index"] == upstream_index + 1]
        assert fragments == argument_fragments[upstream_index]
        assert isinstance(json.loads("".join(fragments)), dict)
    assert [e["index"] for e in events if e["type"] == "content_block_stop"] == [0, 1, 2]
    assert events[-2] == {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                          "usage": events[0]["message"]["usage"]}
    assert events[-1] == {"type": "message_stop"}


def test_sse_buffers_until_real_usage_and_handles_empty_completion():
    consumed = []

    def upstream():
        for value in split_bytes(sse([chunk({"role": "assistant"}), chunk(finish="stop"),
                                     chunk(choices=False, usage=usage())]), 13):
            consumed.append(value)
            yield value

    output = protocol.translate_sse(upstream(), prepared(stream=True))
    first = next(output)
    assert b"message_start" in first
    assert b"[DONE]" in b"".join(consumed)
    events = events_from([first, *output])
    assert [e["type"] for e in events] == ["message_start", "message_delta", "message_stop"]
    assert events[0]["message"]["usage"]["input_tokens"] == 8


def test_sse_text_length_finish_and_multi_line_data_event():
    head = json.dumps(chunk({"role": "assistant", "content": "a"})).encode()
    # Splitting JSON between object fields is valid multi-line SSE data.
    head = head.replace(b', "object":', b',\ndata: "object":', 1)
    raw = b"data: " + head + b"\n\n" + sse([chunk({"content": "b"}, finish="length"),
                                               chunk(choices=False, usage=usage())])
    events = events_from(protocol.translate_sse(split_bytes(raw), prepared(stream=True)))
    assert [e["delta"]["text"] for e in events if e.get("delta", {}).get("type") == "text_delta"] == ["a", "b"]
    assert events[-2]["delta"]["stop_reason"] == "max_tokens"


@pytest.mark.parametrize("data, code", [
    (sse([chunk(finish="stop"), chunk(choices=False, usage=usage())], done=False), "incomplete_stream"),
    (sse([chunk({"content": "x"})]), "incomplete_stream"),
    (sse([chunk(finish="stop")]), "invalid_upstream"),
    (b"data: {bad}\n\n", "invalid_upstream"),
    (b"data: \xff\n\n", "invalid_upstream"),
    (b"data: [DONE]", "incomplete_stream"),
    (b"event: error\ndata: secret vendor detail\n\n", "upstream_error"),
    (sse([{"error": {"message": "private"}}]), "upstream_error"),
    (sse([chunk(model="unexpected")]), "model_mismatch"),
    (sse([chunk(finish="stop"), chunk({"content": "late"})]), "invalid_upstream"),
    (sse([chunk(finish="stop")]) + b"data: {}\n\n", "invalid_upstream"),
    (sse([chunk({"reasoning_content": "hidden"})]), "unsupported_feature"),
    (sse([chunk({"role": "user"})]), "invalid_upstream"),
])
def test_malformed_or_incomplete_sse_never_emits_success(data, code):
    assert_no_emission(split_bytes(data, 3), code=code)


@pytest.mark.parametrize("calls, tail, code", [
    ([{"index": 0, **call()}, {"index": 0, **call("other")}], [], "invalid_upstream"),
    ([{"index": 1, **call()}], [], "invalid_upstream"),
    ([{"index": True, **call()}], [], "invalid_upstream"),
    ([{"index": 0, **call(arguments="{bad}")}], [], "invalid_upstream"),
    ([{"index": 0, **call(arguments="{}")}], [chunk({"tool_calls": [{"index": 0, **call()}]})], "invalid_upstream"),
    ([{"index": 0, **call(arguments="{}")}], [chunk({"content": "after tool"})], "unsupported_feature"),
    ([{"index": 0, "function": {"arguments": "{}"}}], [], "invalid_upstream"),
])
def test_streaming_tool_metadata_indices_and_arguments_are_validated(calls, tail, code):
    data = sse([chunk({"tool_calls": calls}), *tail, chunk(finish="tool_calls"),
                chunk(choices=False, usage=usage())])
    assert_no_emission(split_bytes(data), code=code)


def test_interrupted_upstream_iterator_is_consumed_once_without_output_or_retry():
    seen = []

    def interrupted():
        seen.append("first")
        yield sse([chunk({"content": "partial"})], done=False)
        seen.append("disconnect")
        raise ConnectionError("synthetic disconnection")

    with pytest.raises(ConnectionError):
        next(protocol.translate_sse(interrupted(), prepared(stream=True)))
    assert seen == ["first", "disconnect"]


def test_size_and_nesting_bounds_apply_before_output(monkeypatch):
    request = prepared(stream=True)
    monkeypatch.setattr(protocol, "MAX_REQUEST_BYTES", 20)
    with pytest.raises(protocol.DevinProtocolError) as error:
        prepared()
    assert error.value.code == "limit_exceeded"
    monkeypatch.setattr(protocol, "MAX_SSE_CHUNK_BYTES", 10)
    assert_no_emission([b"x" * 11], request=request, code="limit_exceeded")
    monkeypatch.setattr(protocol, "MAX_SSE_CHUNK_BYTES", 1024)
    monkeypatch.setattr(protocol, "MAX_SSE_EVENT_BYTES", 20)
    assert_no_emission([b"data: " + b"x" * 21], request=request, code="limit_exceeded")


def test_json_depth_and_individual_tool_argument_bounds(monkeypatch):
    nested = {}
    for _ in range(protocol.MAX_JSON_DEPTH + 1):
        nested = {"child": nested}
    with pytest.raises(protocol.DevinProtocolError) as error:
        prepared(tools=[{"name": "Nested", "input_schema": {"type": "object", "properties": nested}}])
    assert error.value.code == "limit_exceeded"
    request = prepared()
    monkeypatch.setattr(protocol, "MAX_TOOL_ARGUMENT_BYTES", 4)
    with pytest.raises(protocol.DevinProtocolError) as error:
        protocol.translate_response(completed(calls=[call()], finish="tool_calls"), request)
    assert error.value.code == "limit_exceeded"


def test_unknown_usage_is_never_replaced_with_zero():
    with pytest.raises(protocol.DevinProtocolError, match="usage"):
        protocol.translate_response(completed(usage=None), prepared())
    assert_no_emission([sse([chunk(finish="stop")])], code="invalid_upstream")


@pytest.mark.parametrize("choice, calls", [
    ({"type": "none"}, [call()]),
    ({"type": "any"}, []),
    ({"type": "tool", "name": "Read"}, [call(name="Bash", arguments="{}")]),
    ({"type": "tool", "name": "Read"}, []),
    ({"type": "auto", "disable_parallel_tool_use": True}, [call(), call("call_second")]),
])
def test_upstream_tool_choice_violations_never_become_executable_tools(choice, calls):
    request = prepared(tool_choice=choice)
    finish = "tool_calls" if calls else "stop"
    with pytest.raises(protocol.DevinProtocolError) as error:
        protocol.translate_response(completed(calls=calls, finish=finish), request)
    assert error.value.code == "invalid_upstream"
    deltas = {"tool_calls": [{"index": i, **item} for i, item in enumerate(calls)]}
    data = sse([chunk(deltas), chunk(finish=finish), chunk(choices=False, usage=usage())])
    assert_no_emission(split_bytes(data), request=request, code="invalid_upstream")


def test_selected_nonparallel_tool_is_accepted_by_both_response_translators():
    request = prepared(tool_choice={"type": "tool", "name": "Read", "disable_parallel_tool_use": True})
    result = protocol.translate_response(completed(calls=[call()], finish="tool_calls"), request)
    assert result["content"][-1]["name"] == "Read"
    data = sse([chunk({"tool_calls": [{"index": 0, **call()}]}), chunk(finish="tool_calls"),
                chunk(choices=False, usage=usage())])
    assert events_from(protocol.translate_sse(split_bytes(data), request))[-2]["delta"]["stop_reason"] == "tool_use"


@pytest.mark.parametrize("prefix", ["", "-"])
def test_json_integer_lexemes_are_bounded_before_decimal_conversion(monkeypatch, prefix):
    # Exercise the parser guard with a short synthetic lexeme rather than
    # performing an expensive conversion of a multi-megabyte decimal integer.
    monkeypatch.setattr(protocol, "MAX_JSON_INTEGER_DIGITS", 8)
    with pytest.raises(protocol.DevinProtocolError) as error:
        protocol._load(b'{"n":' + prefix.encode() + b"123456789}", 100, "invalid_request")
    assert error.value.code == "limit_exceeded"
    assert protocol._load(b'{"n":' + prefix.encode() + b"12345678}", 100,
                          "invalid_request") == {"n": -12345678 if prefix else 12345678}

    def forbidden_conversion(_value):
        pytest.fail("oversized integer lexeme reached decimal conversion")

    with monkeypatch.context() as local:
        local.setattr(protocol, "int", forbidden_conversion, raising=False)
        with pytest.raises(protocol.DevinProtocolError) as error:
            protocol._parse_integer(prefix + "123456789")
        assert error.value.code == "limit_exceeded"


@pytest.mark.parametrize("sign", [1, -1])
def test_already_decoded_big_integers_fail_before_json_serialization(monkeypatch, sign):
    maximum = protocol.MAX_JSON_INTEGER
    assert protocol._load({"n": sign * maximum}, 2000, "invalid_request") == {"n": sign * maximum}

    def forbidden_dump(_value):
        pytest.fail("oversized Python integer reached decimal JSON serialization")

    monkeypatch.setattr(protocol, "_dump", forbidden_dump)
    with pytest.raises(protocol.DevinProtocolError) as error:
        protocol._load({"n": sign * (maximum + 1)}, 2000, "invalid_request")
    assert error.value.code == "limit_exceeded"

