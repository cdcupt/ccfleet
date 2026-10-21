"""Generic caller-tool MCP transport with fake bytes/private fixtures only."""

from __future__ import annotations

import importlib.util
import io
import json
import marshal
import os
import struct
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from ccfleet_agent import devin_mcp as mcp
from ccfleet_agent.devin_tools import ToolCatalog

PROCESS = {"pid": 1234, "pgid": 1200, "startticks": 5678}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


def initialize(version="2025-11-25"):
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "fake"}}}


def call(name="Read", arguments=None, rpc_id="call-1"):
    return {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call", "params": {
        "name": name, "arguments": {"file_path": "/caller/only"} if arguments is None else arguments}}


@pytest.fixture
def private(tmp_path):
    tmp_path.chmod(0o700)
    tools = [{"name": name, "description": "Exact caller " + name + " 日本語",
              "input_schema": {"type": "object", "properties": {"value": {"type": "string"}},
                               "additionalProperties": False}} for name in ("Read", "Edit", "Bash")]
    config = {"tools": tools, "binding": "owner:key:claim:provider-generation",
              "session_id": None, "server_id": "owned-connection", "mcp_server_name": "ccfleet"}

    def write(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    write("config.json", config)
    bound = {"binding": config["binding"], "server_id": config["server_id"],
             "session_id": "native-owned", "schema_digest": ToolCatalog(tools).schema_digest}

    def bind(**changes):
        write("session.json", dict(bound, **changes))

    def serve(*messages, cancelled=None, output=None):
        raw = b"".join(json.dumps(message, ensure_ascii=False).encode() + b"\n"
                       for message in messages)
        stream = io.BytesIO() if output is None else output
        result = mcp.serve(tmp_path, io.BytesIO(raw), stream,
                           identity=lambda: dict(PROCESS), cancelled=cancelled)
        return result, [json.loads(line) for line in stream.getvalue().splitlines()]

    def hook(name, arguments, **changes):
        event = {"hook_event_name": "PreToolUse", "tool_name": name,
                 "tool_input": arguments, "session_id": "PRIVATE_SESSION"}
        event.update(changes)
        output = io.BytesIO()
        code = mcp.hook(tmp_path, io.BytesIO(json.dumps(event).encode()), output)
        return code, json.loads(output.getvalue())

    return tmp_path, config, write, bind, serve, hook


def test_handshake_lists_exact_frozen_catalog_before_session_binding(private):
    path, config, _, _, serve, _ = private
    code, replies = serve(initialize(), INITIALIZED, LIST)
    assert code == 0
    assert replies[1]["result"] == ToolCatalog(config["tools"]).tools_list()
    assert json.loads((path / "process.json").read_text()) == PROCESS
    assert not (path / "call.json").exists()
    entries = [json.loads(line) for line in (path / "lifecycle.jsonl").read_text().splitlines()]
    assert any(e["reason"] == "initialize_responded" and e["initialized"] for e in entries)
    assert any(e["reason"] == "initialized" and e["ready"] for e in entries)
    assert any(e["reason"] == "tools_list_responded" and e["ready"] for e in entries)


def test_catalog_over_128_tools_survives_real_mcp_discovery_and_last_tool_handoff(private):
    path, config, write, _, serve, _ = private
    config["tools"] = [
        {"name": f"mcp__synthetic__Tool{index}", "description": "Synthetic local tool.",
         "input_schema": {"type": "object", "properties": {}}} for index in range(160)]
    catalog = ToolCatalog(config["tools"])
    write("config.json", config)
    write("session.json", {"binding": config["binding"], "server_id": config["server_id"],
          "session_id": "native-owned", "schema_digest": catalog.schema_digest})
    last_name = config["tools"][-1]["name"]
    code, replies = serve(initialize(), INITIALIZED, LIST, call(last_name, {"value": "local"}))
    assert code == 1 and replies[1]["result"] == catalog.tools_list()
    receipt = json.loads((path / "call.json").read_text())
    assert receipt["frontend_tool_use"]["name"] == last_name
    assert receipt["frontend_tool_use"]["input"] == {"value": "local"}


@pytest.mark.parametrize("name,arguments", [
    ("Read", {"file_path": "/caller/only", "offset": 1}),
    ("Edit", {"file_path": "/caller/file", "old_string": "OLD\n日本語", "new_string": "NEW\n"}),
    ("Bash", {"command": "printf NEVER_EXECUTE; touch /node/NEVER", "timeout": 1000}),
])
def test_generic_handoff_preserves_original_arguments_without_executing_any_tool(
        private, monkeypatch, name, arguments):
    path, _, _, bind, serve, _ = private
    bind()
    monkeypatch.setattr(os, "system", lambda *args: pytest.fail("tool executed on node"))
    code, replies = serve(initialize(), INITIALIZED, LIST, call(name, arguments, 7))
    assert code == 1 and [reply["id"] for reply in replies] == [1, 2]
    receipt = json.loads((path / "call.json").read_text())
    assert receipt["frontend_tool_use"]["name"] == name
    assert receipt["frontend_tool_use"]["input"] == arguments
    assert receipt["native_rpc_id"] == 7 and type(receipt["native_rpc_id"]) is int
    assert receipt["context"]["session_id"] == "native-owned" and receipt["process"] == PROCESS
    assert receipt["frontend_tool_use"]["id"].startswith("toolu_devin_mcp_")
    assert (path / "call.json").stat().st_mode & 0o777 == 0o600
    assert list(path.glob(".pending-*")) == []


@pytest.mark.parametrize("version", [*sorted(mcp.VERSIONS), "2026-07-28"])
def test_negotiation_echoes_supported_versions_and_offers_latest_for_future(private, version):
    _, _, _, _, serve, _ = private
    _, replies = serve(initialize(version), INITIALIZED, LIST)
    assert replies[0]["result"]["protocolVersion"] == (
        version if version in mcp.VERSIONS else mcp.LATEST_VERSION)


def test_preinitialize_discovery_is_noop_fallback_and_late_discovery_is_error(private):
    path, _, _, _, serve, _ = private
    discover = {"jsonrpc": "2.0", "id": "discover", "method": "server/discover", "params": {}}
    _, replies = serve(discover, initialize("2026-07-28"), INITIALIZED, LIST,
                       dict(discover, id="late"))
    assert replies[0]["error"]["code"] == -32601 and replies[-1]["error"]["code"] == -32601
    text = (path / "lifecycle.jsonl").read_text()
    assert text.count('"reason":"modern_discovery_fallback"') == 1
    assert '"reason":"request_rejected"' in text


def test_opaque_protocol_metadata_is_stripped_from_arguments_and_private_logs(private):
    path, _, _, bind, serve, _ = private
    bind()
    request = call("Bash", {"command": "PRIVATE_COMMAND"})
    request["params"]["_meta"] = {"progressToken": "PRIVATE_TOKEN"}
    start = initialize()
    start["params"]["_meta"] = {"trace": "PRIVATE_TRACE"}
    start["params"]["clientInfo"] = {"name": "PRIVATE_CLIENT"}
    serve(start, INITIALIZED, request)
    receipt = json.loads((path / "call.json").read_text())
    assert receipt["frontend_tool_use"]["input"] == {"command": "PRIVATE_COMMAND"}
    assert "PRIVATE" not in (path / "lifecycle.jsonl").read_text()


@pytest.mark.parametrize("change", [{"name": "exec"}, {"arguments": []}, {"extra": True}])
def test_undeclared_or_malformed_calls_cannot_create_handoffs(private, change):
    path, _, _, bind, serve, _ = private
    bind()
    request = call()
    request["params"].update(change)
    _, replies = serve(initialize(), INITIALIZED, request)
    assert replies[-1]["error"]["code"] == -32602
    assert not (path / "call.json").exists()


@pytest.mark.parametrize("field", ["binding", "server_id", "schema_digest", "session_id"])
def test_final_binding_and_catalog_digest_are_required(private, field):
    path, _, _, bind, serve, _ = private
    bind(**{field: "foreign" if field != "session_id" else "bad session"})
    _, replies = serve(initialize(), INITIALIZED, call())
    assert "error" in replies[-1] and not (path / "call.json").exists()


def test_unbound_session_cannot_call_tools(private):
    path, _, _, _, serve, _ = private
    _, replies = serve(initialize(), INITIALIZED, call())
    assert "error" in replies[-1] and not (path / "call.json").exists()


def test_second_call_and_id_reuse_cannot_complete_or_replace_first_handoff(private):
    path, _, _, bind, serve, _ = private
    bind()
    output = io.BytesIO()
    with pytest.raises(mcp.Rejected):
        serve(initialize(), INITIALIZED, call(rpc_id="pending"), call(rpc_id="second"),
              {"jsonrpc": "2.0", "id": "pending", "method": "ping"}, output=output)
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [reply["id"] for reply in replies] == [1, "second"]
    assert json.loads((path / "call.json").read_text())["native_rpc_id"] == "pending"


@pytest.mark.parametrize("request_id", [3, "3"])
def test_matching_typed_cancellation_does_not_return_a_tool_result(private, request_id):
    path, _, _, bind, serve, _ = private
    bind()
    _, replies = serve(initialize(), INITIALIZED, call(rpc_id=request_id), {
        "jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": request_id}})
    assert [reply["id"] for reply in replies] == [1]
    assert (path / "call.json").exists()


def test_foreign_typed_cancellation_fails(private):
    _, _, _, bind, serve, _ = private
    bind()
    with pytest.raises(mcp.Rejected):
        serve(initialize(), INITIALIZED, call(rpc_id=3), {
            "jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "3"}})


@pytest.mark.parametrize("name,allowed", [("mcp__ccfleet__Read", True),
    ("mcp__ccfleet__Edit", True), ("mcp__ccfleet__Bash", True), ("read", False),
    ("Read", False), ("Bash", False), ("exec", False), ("mcp__other__Read", False),
    ("mcp__ccfleet__BashExtra", False), ("mcp__ccfleet__Unknown", False)])
def test_hook_grants_exact_catalog_namespace_only_and_logs_no_arguments(private, name, allowed):
    path, _, _, _, _, hook = private
    code, result = hook(name, {"command": "PRIVATE_ARGUMENT"})
    assert code == (0 if allowed else 2)
    assert result == {"decision": "approve" if allowed else "block"}
    assert "PRIVATE" not in (path / "hook.jsonl").read_text()


@pytest.mark.parametrize("name,arguments,allowed", [
    ("mcp_list_tools", {"server_name": "ccfleet"}, True),
    ("mcp_list_tools", {"server_name": "other"}, False),
    ("mcp_list_tools", {"server_name": "ccfleet", "extra": True}, False),
    ("mcp_call_tool", {"server_name": "ccfleet", "tool_name": "Bash", "arguments": {"command": "X"}}, True),
    ("mcp_call_tool", {"server_name": "ccfleet", "tool_name": "exec", "arguments": {}}, False),
    ("mcp_call_tool", {"server_name": "other", "tool_name": "Read", "arguments": {}}, False),
    ("mcp_call_tool", {"server_name": "ccfleet", "tool_name": "Read", "arguments": []}, False),
])
def test_discovery_and_wrapper_hooks_are_strictly_server_and_catalog_scoped(
        private, name, arguments, allowed):
    _, _, _, _, _, hook = private
    code, result = hook(name, arguments)
    assert code == (0 if allowed else 2) and result["decision"] == ("approve" if allowed else "block")


@pytest.mark.parametrize("field,value", [("mcp_server_name", "other"), ("tools", []),
                                         ("binding", "bad\nowner")])
def test_invalid_catalog_or_server_config_rejects_before_handshake(private, field, value):
    path, config, write, _, serve, _ = private
    config[field] = value
    write("config.json", config)
    with pytest.raises((mcp.Rejected, ValueError)):
        serve(initialize())
    assert not (path / "process.json").exists()


def test_private_paths_permissions_and_exclusive_receipts_are_enforced(private):
    path, _, _, _, serve, _ = private
    config = path / "config.json"
    config.chmod(0o644)
    with pytest.raises(mcp.Rejected):
        serve(initialize())
    config.chmod(0o600)
    target = path / "actual.json"
    config.rename(target)
    config.symlink_to(target)
    with pytest.raises(OSError):
        serve(initialize())
    config.unlink()
    target.rename(config)
    (path / "process.json").write_text("existing")
    (path / "process.json").chmod(0o600)
    with pytest.raises(FileExistsError):
        serve(initialize())


def test_cancelled_server_never_admits_a_tool(private):
    path, _, _, bind, serve, _ = private
    bind()
    stop = threading.Event()
    stop.set()
    code, replies = serve(initialize(), INITIALIZED, call(), cancelled=stop)
    assert code == 0 and replies == [] and not (path / "call.json").exists()


def test_json_integer_duplicate_and_wire_bounds_are_not_silently_accepted(private, monkeypatch):
    path, _, _, _, _, _ = private
    for raw in (b'{"id":1,"id":2}', b'{"n":NaN}', b'{"n":' + b'1' * 1025 + b'}'):
        with pytest.raises(mcp.Rejected):
            mcp._decode(raw)
    monkeypatch.setattr(mcp, "MAX_LINE", 64)
    with pytest.raises(mcp.Rejected):
        mcp.serve(path, io.BytesIO(b"x" * 65), io.BytesIO(), identity=lambda: dict(PROCESS))


def test_lifecycle_log_is_private_and_bounded(private):
    path, _, _, _, serve, _ = private
    log = path / "lifecycle.jsonl"
    log.write_bytes(b"x" * mcp.MAX_LIFECYCLE)
    log.chmod(0o600)
    with pytest.raises(mcp.Rejected):
        serve(initialize())
    assert log.stat().st_size == mcp.MAX_LIFECYCLE


def test_hook_name_export_contains_no_bare_native_grants(private):
    _, config, _, _, _, _ = private
    assert mcp.hook_tool_names(ToolCatalog(config["tools"])) == (
        "mcp__ccfleet__Read", "mcp__ccfleet__Edit", "mcp__ccfleet__Bash")


def test_lifecycle_reader_validates_real_server_records_before_and_after_close(private):
    path, _, _, bind, _, _ = private
    bind()
    messages = [initialize("2026-07-28"), INITIALIZED, LIST, call("Edit", {"new_string": "X"})]
    chunks = [json.dumps(message).encode() + b"\n" for message in messages]
    observed = []

    class Input:
        def read(self, maximum):
            if chunks:
                return chunks.pop(0)
            observed.append(mcp.read_lifecycle(path))
            return b""

    assert mcp.read_lifecycle(path)["phase"] == "missing"
    code = mcp.serve(path, Input(), io.BytesIO(), identity=lambda: dict(PROCESS))
    assert code == 1
    assert observed[0]["ready"] and observed[0]["handshake_complete"]
    assert observed[0]["protocol_version"] == "2025-11-25"
    assert observed[0]["tools_list_count"] == 1
    with pytest.raises(mcp.Rejected, match="mcp_shim_closed"):
        mcp.read_lifecycle(path)


def lifecycle(reason, method="[unparsed]", initialized=False, ready=False, protocol=None, id_key=False):
    entry = {"reason": reason, "method": method, "initialized": initialized, "ready": ready,
             "keys": ["jsonrpc", "method"] + (["id"] if id_key else []), "params_keys": []}
    if protocol is not None:
        entry["protocolVersion"] = protocol
    return entry


def live_entries():
    return [lifecycle("started"),
            lifecycle("initialize_responded", "initialize", True, False, "2025-11-25", True),
            lifecycle("initialized", "notifications/initialized", True, True, "2025-11-25")]


@pytest.mark.parametrize("entries,code", [
    ([lifecycle("invalid_protocol_version")], "mcp_invalid_protocol_version"),
    ([lifecycle("request_rejected")], "mcp_request_rejected"),
    ([lifecycle("initialized", "notifications/initialized", True, True, "2025-11-25")],
     "mcp_ready_proof_invalid"),
    ([lifecycle("modern_discovery_fallback", "server/discover")], "mcp_discovery_fallback_invalid"),
    ([*live_entries(), lifecycle("modern_discovery_fallback", "server/discover", id_key=True)],
     "mcp_discovery_fallback_invalid"),
])
def test_lifecycle_reader_rejects_errors_invalid_proofs_and_late_fallback(private, entries, code):
    path, _, _, _, _, _ = private
    log = path / "lifecycle.jsonl"
    log.write_bytes(b"".join(json.dumps(entry).encode() + b"\n" for entry in entries))
    log.chmod(0o600)
    with pytest.raises(mcp.Rejected, match=code):
        mcp.read_lifecycle(path)


def test_lifecycle_reader_accepts_only_valid_preinit_fallback_and_defers_partial_append(private):
    path, _, _, _, _, _ = private
    entries = live_entries()
    entries.insert(1, lifecycle("modern_discovery_fallback", "server/discover", id_key=True))
    raw = b"".join(json.dumps(entry).encode() + b"\n" for entry in entries)
    log = path / "lifecycle.jsonl"
    log.write_bytes(raw)
    log.chmod(0o600)
    assert mcp.read_lifecycle(path)["ready"]
    log.write_bytes(raw + b'{"incomplete"')
    value = mcp.read_lifecycle(path)
    assert value["phase"] == "partial" and value["handshake_complete"] and not value["ready"]
    log.chmod(0o644)
    with pytest.raises(mcp.Rejected, match="mcp_lifecycle_file_guard"):
        mcp.read_lifecycle(path)


def test_stalled_real_output_pipe_is_nonblocking_and_deadline_bounded():
    read_fd, write_fd = os.pipe()
    try:
        os.set_blocking(write_fd, False)
        while True:
            try:
                os.write(write_fd, b"x" * 4096)
            except BlockingIOError:
                break
        now = [0.0]

        def clock():
            now[0] += 0.1
            return now[0]

        with os.fdopen(write_fd, "wb", buffering=0, closefd=False) as output:
            with pytest.raises(mcp.Rejected, match="output stopped"):
                mcp._send(output, {"jsonrpc": "2.0", "id": 1, "result": {}},
                          clock=clock, deadline=0.4)
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_isolated_direct_script_defaults_to_plan_without_importing_user_modules(tmp_path):
    (tmp_path / "devin_tools.py").write_text("raise RuntimeError('USER_IMPORT_MUST_NOT_RUN')")
    result = subprocess.run([sys.executable, "-I", mcp.__file__, "--plan"], cwd=tmp_path,
                            env={"HOME": str(tmp_path), "PYTHONPATH": str(tmp_path)},
                            capture_output=True, timeout=5)
    assert result.returncode == 0 and result.stderr == b""
    value = json.loads(result.stdout)
    assert value["execute"] is False and value["node_tool_execution"] is False
    assert value["native_model_calls"] is False
    assert list(tmp_path.iterdir()) == [tmp_path / "devin_tools.py"]


def test_isolated_entry_compiles_pinned_sibling_source_instead_of_unverified_bytecode(
        tmp_path, monkeypatch):
    script = tmp_path / "devin_mcp.py"
    script.write_bytes(Path(mcp.__file__).read_bytes())
    source = tmp_path / "devin_tools.py"
    source.write_bytes(Path(mcp.__file__).with_name("devin_tools.py").read_bytes())
    prefix = tmp_path / "cache"
    with monkeypatch.context() as context:
        context.setattr(sys, "pycache_prefix", str(prefix))
        cache = Path(importlib.util.cache_from_source(str(source)))
    cache.parent.mkdir(parents=True)
    info = source.stat()
    header = importlib.util.MAGIC_NUMBER + struct.pack("<III", 0, int(info.st_mtime), info.st_size)
    cache.write_bytes(header + marshal.dumps(compile(
        "raise RuntimeError('UNVERIFIED_CACHE_MUST_NOT_EXECUTE')", str(source), "exec")))
    result = subprocess.run([sys.executable, "-I", "-X", "pycache_prefix=" + str(prefix),
                             str(script), "--plan"],
                            capture_output=True, timeout=5)
    assert result.returncode == 0 and result.stderr == b""
    assert json.loads(result.stdout)["execute"] is False
