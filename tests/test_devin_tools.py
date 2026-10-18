"""Pure generic MCP tool handoffs; no native/provider/tool/transport calls."""

from __future__ import annotations

import copy
import dataclasses
import threading

import pytest

from ccfleet_agent import devin_tools as bridge

BINDING = "verified-owner:key:generation-7"
SESSION = "owned-native-session"
SERVER = "owned-mcp-connection"
IDENTITY = {"binding": BINDING, "session_id": SESSION, "server_id": SERVER}
CONTEXT = dict(IDENTITY, request_id="native-1")
CAPTURED_NAMES = [
    "Agent",
    "Bash",
    "CronCreate",
    "CronDelete",
    "CronList",
    "Edit",
    "EnterWorktree",
    "ExitWorktree",
    "ListAgents",
    "NotebookEdit",
    "Read",
    "ReportFindings",
    "ScheduleWakeup",
    "SendMessage",
    "Skill",
    "TaskStop",
    "WebFetch",
    "WebSearch",
    "Workflow",
    "Write",
]


def declarations():
    return [
        {
            "name": name,
            "description": "Declared " + name + " tool.\nKeep its schema.",
            "input_schema": {
                "type": "object",
                "properties": {"value": {"type": "string", "description": "text 日本語"}},
                "additionalProperties": False,
            },
        }
        for name in ("Read", "Bash", "Agent")
    ]


def request(**changes):
    value = {
        "jsonrpc": "2.0",
        "id": "native-1",
        "method": "tools/call",
        "params": {"name": "Read", "arguments": {"value": "client/path 日本語"}},
    }
    value.update(changes)
    return value


def new_handoff(**options):
    return bridge.ToolHandoff(bridge.ToolCatalog(declarations()), **IDENTITY, **options)


def pending(**options):
    handoff = new_handoff(**options)
    tool = handoff.begin(request(), **IDENTITY, remaining_seconds=2)
    return handoff, tool


def result(tool, **changes):
    value = {"type": "tool_result", "tool_use_id": tool.tool_id, "content": "  2→日本語\n\t"}
    value.update(changes)
    return value


def test_catalog_freezes_exact_names_descriptions_schemas_and_declaration_order():
    source = declarations()
    original = copy.deepcopy(source)
    catalog = bridge.ToolCatalog(source)
    expected = [
        {"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]}
        for t in original
    ]
    assert catalog.tools_list() == {"tools": expected}
    source[0]["input_schema"]["properties"]["value"]["type"] = "number"
    source[0]["description"] = "changed"
    source.append(source[1])
    exported = catalog.tools_list()
    exported["tools"][0]["inputSchema"]["properties"]["value"]["type"] = "boolean"
    assert catalog.tools_list() == {"tools": expected}
    assert len(catalog.schema_digest) == 64
    assert "text 日本語" not in repr(catalog)
    with pytest.raises(dataclasses.FrozenInstanceError):
        catalog.schema_digest = "changed"


def test_catalog_digest_is_key_order_independent_but_includes_all_declarations():
    source = declarations()
    same = [
        {"input_schema": t["input_schema"], "description": t["description"], "name": t["name"]}
        for t in source
    ]
    catalog = bridge.ToolCatalog(source)
    assert bridge.ToolCatalog(same).schema_digest == catalog.schema_digest
    for field, changed in (
        ("name", "Other"),
        ("description", "Other description"),
        ("input_schema", {"type": "object", "required": ["different"]}),
    ):
        altered = copy.deepcopy(source)
        altered[0][field] = changed
        assert bridge.ToolCatalog(altered).schema_digest != catalog.schema_digest
    assert bridge.ToolCatalog(list(reversed(source))).schema_digest != catalog.schema_digest


def test_twenty_captured_function_tool_names_need_no_builtin_name_translation():
    tools = [
        {"name": name, "description": "Declared tool", "input_schema": {"type": "object"}}
        for name in CAPTURED_NAMES
    ]
    assert [
        tool["name"] for tool in bridge.ToolCatalog(tools).tools_list()["tools"]
    ] == CAPTURED_NAMES


@pytest.mark.parametrize(
    "tools",
    [
        [],
        {},
        [None],
        [{"type": "web_search_20250305", "name": "web_search"}],
        [{"name": "Read", "input_schema": {"type": "object"}}],
        [{"name": "Read", "description": "x", "input_schema": True}],
        [{"name": "Read", "description": "x", "input_schema": {"type": "array"}}],
        [{"name": "Read", "description": 7, "input_schema": {"type": "object"}}],
        [{"name": "Read", "description": "x", "input_schema": {"type": "object"}, "strict": True}],
        [{"name": "bad name", "description": "x", "input_schema": {"type": "object"}}],
        [{"name": "x" * 65, "description": "x", "input_schema": {"type": "object"}}],
        declarations() + [declarations()[0]],
    ],
)
def test_invalid_server_or_unsupported_tool_declarations_fail_explicitly(tools):
    with pytest.raises(bridge.ToolBridgeError):
        bridge.ToolCatalog(tools)


def test_genuine_call_is_suspended_then_one_matching_result_is_delivered():
    handoff, tool = pending()
    assert tool.as_tool_use() == {
        "type": "tool_use",
        "id": tool.tool_id,
        "name": "Read",
        "input": {"value": "client/path 日本語"},
    }
    assert tool.tool_id != "native-1"
    assert "client/path" not in repr(tool)
    received, ready = [], threading.Event()

    def waiter():
        ready.set()
        received.append(handoff.wait(**CONTEXT))

    thread = threading.Thread(target=waiter)
    thread.start()
    assert ready.wait(1)
    handoff.resolve(result(tool), **CONTEXT)
    thread.join(1)
    assert not thread.is_alive()
    assert received == [
        {
            "jsonrpc": "2.0",
            "id": "native-1",
            "result": {"content": [{"type": "text", "text": "  2→日本語\n\t"}], "isError": False},
        }
    ]
    for operation in (
        lambda: handoff.wait(**CONTEXT),
        lambda: handoff.pending(**CONTEXT),
        lambda: handoff.resolve(result(tool), **CONTEXT),
        lambda: handoff.begin(request(), **IDENTITY, remaining_seconds=1),
    ):
        with pytest.raises(bridge.ToolBridgeError) as error:
            operation()
        assert error.value.code == "replay"


def test_nested_arguments_and_exported_tool_use_are_detached():
    handoff = new_handoff()
    native = request(
        params={
            "name": "Agent",
            "arguments": {"nested": [None, True, 0, -2, 1.5, {"text": "\x00\n日本語"}]},
        }
    )
    original = copy.deepcopy(native["params"]["arguments"])
    tool = handoff.begin(native, **IDENTITY, remaining_seconds=2)
    native["params"]["arguments"]["nested"].append("changed")
    exported = tool.as_tool_use()
    exported["input"]["nested"].clear()
    assert handoff.pending(**CONTEXT).as_tool_use()["input"] == original
    handoff.cancel(**CONTEXT)


@pytest.mark.parametrize("rpc_id", [0, 1, -1, "1", "native:call/1"])
def test_mcp_rpc_id_retains_exact_type_and_value(rpc_id):
    handoff = new_handoff()
    context = dict(IDENTITY, request_id=rpc_id)
    tool = handoff.begin(request(id=rpc_id), **IDENTITY, remaining_seconds=2)
    handoff.resolve(result(tool), **context)
    delivered = handoff.wait(**context)
    assert type(delivered["id"]) is type(rpc_id)
    assert delivered["id"] == rpc_id


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"binding": "another-owner:key:generation"}, "binding_mismatch"),
        ({"session_id": "another-session"}, "session_mismatch"),
        ({"server_id": "another-mcp-connection"}, "server_mismatch"),
        ({"request_id": "another-rpc-id"}, "request_mismatch"),
        ({"request_id": 1}, "request_mismatch"),
    ],
)
def test_foreign_binding_session_connection_or_rpc_cannot_inspect_resolve_cancel_or_deliver(
    changes, code
):
    handoff, tool = pending()
    wrong = dict(CONTEXT, **changes)
    for operation in (
        lambda: handoff.pending(**wrong),
        lambda: handoff.resolve(result(tool), **wrong),
        lambda: handoff.cancel(**wrong),
        lambda: handoff.wait(**wrong),
    ):
        with pytest.raises(bridge.ToolBridgeError) as error:
            operation()
        assert error.value.code == code
    handoff.resolve(result(tool), **CONTEXT)
    assert handoff.wait(**CONTEXT)["result"]["isError"] is False


def test_numeric_and_string_equal_rpc_ids_are_not_interchangeable():
    handoff = new_handoff()
    tool = handoff.begin(request(id=1), **IDENTITY, remaining_seconds=2)
    with pytest.raises(bridge.ToolBridgeError) as error:
        handoff.resolve(result(tool), **dict(IDENTITY, request_id="1"))
    assert error.value.code == "request_mismatch"
    handoff.cancel(**dict(IDENTITY, request_id=1))


@pytest.mark.parametrize(
    "changes", [{"binding": "other"}, {"session_id": "other"}, {"server_id": "other"}]
)
def test_wrong_server_context_cannot_admit_a_native_request(changes):
    handoff = new_handoff()
    with pytest.raises(bridge.ToolBridgeError):
        handoff.begin(request(), **dict(IDENTITY, **changes), remaining_seconds=1)
    tool = handoff.begin(request(), **IDENTITY, remaining_seconds=1)
    handoff.cancel(**CONTEXT)
    assert tool.name == "Read"


@pytest.mark.parametrize(
    "native",
    [
        request(method="session/update", params={"sessionUpdate": "tool_call", "name": "Read"}),
        request(method="fs/read_text_file"),
        request(method="tools/list"),
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "Read", "arguments": {}}},
        request(jsonrpc="1.0"),
        request(id=None),
        request(id=True),
        request(id=1.5),
        request(id=[]),
        request(id=1_000_000_001),
        request(extra="unsupported"),
        request(params={"name": "Missing", "arguments": {}}),
        request(params={"name": "read", "arguments": {}}),
        request(params={"name": "Read", "arguments": []}),
        request(params={"name": "Read", "arguments": {}, "sessionId": SESSION}),
        request(params={"name": "Read"}),
    ],
)
def test_only_genuine_declared_mcp_calls_with_object_arguments_can_begin(native):
    handoff = new_handoff()
    with pytest.raises(bridge.ToolBridgeError):
        handoff.begin(copy.deepcopy(native), **IDENTITY, remaining_seconds=1)


def test_one_outstanding_call_and_one_delivery_even_with_concurrent_threads():
    handoff = new_handoff()
    gate, admitted, rejected = threading.Barrier(3), [], []

    def begin():
        gate.wait()
        try:
            admitted.append(handoff.begin(request(), **IDENTITY, remaining_seconds=2))
        except bridge.ToolBridgeError as error:
            rejected.append(error.code)

    threads = [threading.Thread(target=begin) for _ in range(2)]
    for thread in threads:
        thread.start()
    gate.wait()
    for thread in threads:
        thread.join(1)
        assert not thread.is_alive()
    assert len(admitted) == 1 and rejected == ["replay"]
    handoff.resolve(result(admitted[0]), **CONTEXT)
    delivered = []

    def deliver():
        try:
            delivered.append(handoff.wait(**CONTEXT))
        except bridge.ToolBridgeError as error:
            rejected.append(error.code)

    threads = [threading.Thread(target=deliver) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(1)
        assert not thread.is_alive()
    assert len(delivered) == 1 and rejected == ["replay", "replay"]


def test_text_fragments_and_frontend_error_are_preserved_verbatim_as_mcp_tool_result():
    handoff, tool = pending()
    pieces = [
        {"type": "text", "text": "  2→line\n"},
        {"type": "text", "text": "\t\x00"},
        {"type": "text", "text": ""},
    ]
    original = copy.deepcopy(pieces)
    frontend = result(tool, content=pieces, is_error=True)
    handoff.resolve(frontend, **CONTEXT)
    pieces[0]["text"] = "changed"
    delivered = handoff.wait(**CONTEXT)
    assert delivered["result"] == {"content": original, "isError": True}
    assert "error" not in delivered


@pytest.mark.parametrize(
    "content",
    [
        None,
        1,
        {},
        [{"type": "image", "data": "x"}],
        [{"type": "text", "text": 1}],
        [{"type": "text", "text": "x", "cache_control": {}}],
        "\ud800",
    ],
)
def test_rich_or_invalid_results_are_rejected_without_consuming_pending_call(content):
    handoff, tool = pending()
    with pytest.raises(bridge.ToolBridgeError):
        handoff.resolve(result(tool, content=content), **CONTEXT)
    handoff.resolve(result(tool), **CONTEXT)
    assert handoff.wait(**CONTEXT)["result"]["content"][0]["text"] == "  2→日本語\n\t"


@pytest.mark.parametrize(
    "changes",
    [
        {"tool_use_id": "wrong"},
        {"type": "tool_use"},
        {"is_error": 1},
        {"is_error": "false"},
        {"is_error": None},
        {"extra": "unsupported"},
    ],
)
def test_wrong_frontend_id_type_flags_or_fields_are_rejected(changes):
    handoff, tool = pending()
    with pytest.raises(bridge.ToolBridgeError):
        handoff.resolve(result(tool, **changes), **CONTEXT)
    handoff.cancel(**CONTEXT)


def test_cancel_wakes_waiter_and_rejects_late_result_without_replay():
    handoff, tool = pending()
    started, errors = threading.Event(), []

    def waiter():
        started.set()
        try:
            handoff.wait(**CONTEXT)
        except bridge.ToolBridgeError as error:
            errors.append(error.code)

    thread = threading.Thread(target=waiter)
    thread.start()
    assert started.wait(1)
    handoff.cancel(**CONTEXT)
    thread.join(1)
    assert not thread.is_alive() and errors == ["cancelled"]
    with pytest.raises(bridge.ToolBridgeError) as error:
        handoff.resolve(result(tool), **CONTEXT)
    assert error.value.code == "cancelled"


def test_resolved_result_is_still_revocable_before_delivery():
    handoff, tool = pending()
    handoff.resolve(result(tool), **CONTEXT)
    handoff.cancel(**CONTEXT)
    assert handoff._response_json is None
    with pytest.raises(bridge.ToolBridgeError) as error:
        handoff.wait(**CONTEXT)
    assert error.value.code == "cancelled"


@pytest.mark.parametrize("resolved", [False, True])
def test_original_deadline_applies_to_pending_and_resolved_result(monkeypatch, resolved):
    clock = [100.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock[0])
    handoff, tool = pending()
    if resolved:
        handoff.resolve(result(tool), **CONTEXT)
    clock[0] += 3
    with pytest.raises(bridge.ToolBridgeError) as error:
        handoff.wait(**CONTEXT)
    assert error.value.code == "timeout"
    assert handoff._response_json is None


def test_validation_and_delivery_copying_cannot_overrun_deadline(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock[0])
    handoff, tool = pending()
    original = bridge._snapshot

    def slow(value):
        frozen = original(value)
        clock[0] += 3
        return frozen

    monkeypatch.setattr(bridge, "_snapshot", slow)
    with pytest.raises(bridge.ToolBridgeError) as error:
        handoff.resolve(result(tool), **CONTEXT)
    assert error.value.code == "timeout"
    assert handoff._response_json is None


def test_argument_freezing_cannot_admit_a_call_after_deadline(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock[0])
    handoff = new_handoff()
    original = bridge._snapshot

    def slow_arguments(value):
        frozen = original(value)
        if isinstance(value, dict) and "value" in value:
            clock[0] += 3
        return frozen

    monkeypatch.setattr(bridge, "_snapshot", slow_arguments)
    with pytest.raises(bridge.ToolBridgeError) as error:
        handoff.begin(request(), **IDENTITY, remaining_seconds=2)
    assert error.value.code == "timeout"
    assert handoff._state == "new"


def test_final_response_decode_is_also_inside_deadline(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock[0])
    handoff, tool = pending()
    handoff.resolve(result(tool), **CONTEXT)
    original = bridge.json.loads

    def slow(value):
        decoded = original(value)
        clock[0] += 3
        return decoded

    monkeypatch.setattr(bridge.json, "loads", slow)
    with pytest.raises(bridge.ToolBridgeError) as error:
        handoff.wait(**CONTEXT)
    assert error.value.code == "timeout"
    assert handoff._response_json is None


def test_text_byte_and_fragment_limits_fail_without_truncation():
    handoff, tool = pending(max_result_bytes=6)
    with pytest.raises(bridge.ToolBridgeError) as error:
        handoff.resolve(result(tool, content="日本語"), **CONTEXT)
    assert error.value.code == "limit_exceeded"
    handoff.resolve(result(tool, content="日本"), **CONTEXT)
    assert handoff.wait(**CONTEXT)["result"]["content"] == [{"type": "text", "text": "日本"}]
    handoff, tool = pending()
    with pytest.raises(bridge.ToolBridgeError) as error:
        handoff.resolve(
            result(
                tool, content=[{"type": "text", "text": ""}] * (bridge.MAX_RESULT_FRAGMENTS + 1)
            ),
            **CONTEXT,
        )
    assert error.value.code == "limit_exceeded"
    handoff.cancel(**CONTEXT)


@pytest.mark.parametrize(
    "arguments",
    [
        {"x": float("nan")},
        {"x": float("inf")},
        {"x": b"unsupported"},
        {"x": {1, 2}},
        {1: "non-string key"},
        {"x": 1 << 3402},
    ],
)
def test_nonjson_and_oversized_integer_call_arguments_are_rejected(arguments):
    handoff = new_handoff()
    with pytest.raises(bridge.ToolBridgeError):
        handoff.begin(
            request(params={"name": "Read", "arguments": arguments}),
            **IDENTITY,
            remaining_seconds=1,
        )


def test_json_depth_nodes_text_encoding_and_catalog_limits(monkeypatch):
    handoff = new_handoff()
    nested = {"x": {"x": {"x": {"x": {"x": 1}}}}}
    with monkeypatch.context() as scoped:
        scoped.setattr(bridge, "MAX_JSON_DEPTH", 5)
        with pytest.raises(bridge.ToolBridgeError, match="structure exceeds"):
            handoff.begin(
                request(params={"name": "Read", "arguments": nested}),
                **IDENTITY,
                remaining_seconds=1,
            )
    with monkeypatch.context() as scoped:
        scoped.setattr(bridge, "MAX_JSON_NODES", 30)
        with pytest.raises(bridge.ToolBridgeError, match="structure exceeds"):
            handoff.begin(
                request(params={"name": "Read", "arguments": {"many": [0] * 40}}),
                **IDENTITY,
                remaining_seconds=1,
            )
    with monkeypatch.context() as scoped:
        scoped.setattr(bridge, "MAX_JSON_TEXT_BYTES", 64)
        with pytest.raises(bridge.ToolBridgeError, match="exceeds bound"):
            handoff.begin(
                request(params={"name": "Read", "arguments": {"large": "x" * 65}}),
                **IDENTITY,
                remaining_seconds=1,
            )
    with monkeypatch.context() as scoped:
        scoped.setattr(bridge, "MAX_JSON_BYTES", 200)
        with pytest.raises(bridge.ToolBridgeError, match="exceeds bound"):
            handoff.begin(
                request(params={"name": "Read", "arguments": {"escaped": "\x00" * 40}}),
                **IDENTITY,
                remaining_seconds=1,
            )
    with pytest.raises(bridge.ToolBridgeError):
        bridge.ToolCatalog([declarations()[0]] * (bridge.MAX_TOOLS + 1))
    too_long = declarations()
    too_long[0]["description"] = "x" * (bridge.MAX_DESCRIPTION_BYTES + 1)
    with pytest.raises(bridge.ToolBridgeError, match="description exceeds"):
        bridge.ToolCatalog(too_long)


@pytest.mark.parametrize(
    "value,valid", [(2**20 - 1, True), (-(2**20 - 1), True), (2**20, False), (-(2**20), False)]
)
def test_signed_integer_bound_before_json_encoding(monkeypatch, value, valid):
    handoff = new_handoff()
    monkeypatch.setattr(bridge, "MAX_INTEGER_BITS", 20)
    native = request(params={"name": "Read", "arguments": {"n": value}})
    if valid:
        tool = handoff.begin(native, **IDENTITY, remaining_seconds=1)
        assert tool.as_tool_use()["input"] == {"n": value}
        handoff.cancel(**CONTEXT)
    else:
        with pytest.raises(bridge.ToolBridgeError) as error:
            handoff.begin(native, **IDENTITY, remaining_seconds=1)
        assert error.value.code == "limit_exceeded"


@pytest.mark.parametrize("remaining", [0, -1, True, float("nan"), float("inf"), 61])
def test_invalid_deadlines_never_admit_a_call(remaining):
    with pytest.raises(bridge.ToolBridgeError):
        new_handoff().begin(request(), **IDENTITY, remaining_seconds=remaining)


def test_errors_do_not_echo_arguments_binding_or_result_content():
    secret = "PRIVATE_ARGUMENT_NEVER_LOG"
    handoff, tool = pending()
    for operation in (
        lambda: handoff.pending(**dict(CONTEXT, binding=secret)),
        lambda: handoff.resolve(
            result(tool, content=[{"type": "image", "data": secret}]), **CONTEXT
        ),
        lambda: new_handoff().begin(
            request(params={"name": secret, "arguments": {"x": secret}}),
            **IDENTITY,
            remaining_seconds=1,
        ),
    ):
        with pytest.raises(bridge.ToolBridgeError) as error:
            operation()
        assert secret not in str(error.value)
    handoff.cancel(**CONTEXT)
