"""Pure/fake native adapter tests. No native Devin, SSH, auth, or provider calls."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shlex
import sqlite3
import stat
import threading
import types
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from ccfleet_agent import devin_native as native
from tests.test_node_egress_review import protected as protected


def binding():
    return native.OwnershipBinding("owner-1", "key-1", 1, "slot-1", "a" * 32, 2, 3)


def body(tool="Read"):
    return {"model": "opus", "max_tokens": 128000, "stream": True,
            "system": [{"type": "text", "text": "Keep literal device_id=user-text.",
                        "cache_control": {"type": "ephemeral"}}],
            "thinking": {"type": "adaptive", "display": "omitted"},
            "output_config": {"effort": "max"},
            "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
            "messages": [{"role": "user", "content": "Do the task."},
                         {"role": "system", "content": [{"type": "text", "text": "Visible control"}],
                          "output_config": {"effort": "max"}}],
            "tools": [{"name": tool, "description": "A client-local tool.",
                       "input_schema": {"type": "object", "properties": {}}}]}


def validate(value):
    return native.validate_native_request(value, request_models=["opus"], effort="max")


def test_baseline_actual_cli_envelope_preserves_visible_context_and_removes_structured_identity():
    value = body()
    value.update(metadata={"user_id": "PRIVATE"}, device_id="PRIVATE")
    before = copy.deepcopy(value)
    clean = validate(value)
    assert value == before
    assert "metadata" not in clean and "device_id" not in clean
    for field in ("system", "messages", "tools", "thinking", "output_config", "context_management"):
        assert clean[field] == before[field]
    assert "device_id=user-text" in clean["system"][0]["text"]


@pytest.mark.parametrize("kind", ["image", "document", "thinking", "redacted_thinking"])
def test_unsupported_content_and_thinking_signatures_reject_before_generation(kind):
    value = body()
    value["messages"][0]["content"] = [{"type": kind, "text": "secret", "signature": "secret"}]
    with pytest.raises(native.NativeError, match="unsupported_content"):
        validate(value)


@pytest.mark.parametrize("change", [
    {"model": "other"}, {"temperature": 0.2}, {"top_p": 1}, {"stop_sequences": ["stop"]},
    {"tool_choice": {"type": "tool", "name": "Read"}}, {"max_tokens": True},
    {"stream": 1}, {"thinking": {"type": "enabled", "budget_tokens": 500}},
    {"output_config": {"effort": "low"}}, {"output_config": {"format": {"type": "json_schema"}}},
    {"context_management": {"edits": [{"type": "unknown"}]}}
])
def test_nonbaseline_generation_or_structured_output_rejects(change):
    with pytest.raises(native.NativeError):
        validate({**body(), **change})


def test_tool_cache_metadata_is_context_only_and_not_a_new_server_tool():
    value = body("Bash")
    value["tools"][0]["cache_control"] = {"type": "ephemeral"}
    clean = validate(value)
    assert clean == value
    assert set(native._declared_tools(clean)[0]) == {"name", "description", "input_schema"}
    value["tools"][0]["type"] = "web_search_20250305"
    with pytest.raises(native.NativeError, match="unsupported_tool_declaration"):
        validate(value)


def test_text_tool_history_including_errors_and_past_undeclared_tools_is_preserved():
    value = body()
    value["messages"] += [{"role": "assistant", "content": [{"type": "tool_use", "id": "old-id",
        "name": "OldTool", "input": {"command": "local only"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "old-id",
            "content": [{"type": "text", "text": "local error"}], "is_error": True}]}]
    assert validate(value) == value
    value["messages"][-1]["content"][0]["content"] = [{"type": "image", "source": {}}]
    with pytest.raises(native.NativeError, match="unsupported_content"):
        validate(value)


@pytest.mark.parametrize("change", [
    {"key_generation": True}, {"claim_generation": 0}, {"provider_generation": -1},
    {"owner_id": "../owner"}, {"claim_id": "other"}, {"provider": "anthropic"}])
def test_binding_is_provider_owner_key_and_claim_scoped(change):
    with pytest.raises(native.NativeError, match="invalid_ownership_binding"):
        native.OwnershipBinding(**{**asdict(binding()), **change})


def test_binding_digest_and_config_snapshots_are_detached():
    value = asdict(binding())
    made = native.OwnershipBinding.from_dict(value)
    value["owner_id"] = "other"
    assert made == binding() and len(made.digest) == 64
    assert replace(made, key_generation=2).digest != made.digest
    config = native.NativeConfig(Path("/unused"), "a" * 64, '{"enabled":false}')
    changed = config.data
    changed["enabled"] = True
    assert config.data == {"enabled": False}


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    home, state = tmp_path / "home", tmp_path / "state"
    home.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    value = {"enabled": True, "binding": asdict(binding()), "model_uid": "native-max",
             "verified_models": ["native-max"], "request_models": ["opus"],
             "model_state_values": ["native-max"], "thought_level": "max", "timeout_s": 1,
             "files": {name: {"path": "/immutable/" + name} for name in ("executable", "python", "mcp")}}
    config = native.NativeConfig(tmp_path / "unused-policy", "b" * 64, json.dumps(value))
    instances, cleanups, contexts = [], [], []
    flags = {"mode": "tool", "cleanup": True, "revoked": False, "policy_changed": False}

    def policy_guard(_self):
        if flags["policy_changed"]:
            raise native.NativeError("policy_changed")
        return home, state

    monkeypatch.setattr(native.NativeConfig, "validate", policy_guard)
    monkeypatch.setattr(native, "_isolated_subreaper", lambda: None)
    monkeypatch.setattr(native, "_identity", lambda process, pid: None)
    monkeypatch.setattr(native, "_accounting", lambda *_: {"usage": None,
        "unique_finalized_provider_ids": None, "actual_provider_requests": None,
        "served_model_verified": False, "model_mismatch_detected": False})

    def cleanup(pid):
        cleanups.append(pid)
        assert all(instance.closed for instance in instances)
        return flags["cleanup"]

    monkeypatch.setattr(native, "_cleanup_group", cleanup)

    class FakeController:
        def __init__(self, executable, **kwargs):
            self.cwd = Path(kwargs["cwd"])
            self.options, self.executable = kwargs, executable
            self.pid = 2222 + len(instances)
            self._process = types.SimpleNamespace(returncode=None)
            self.closed, self.notices, self.prompted = False, [], False
            self.stopped = threading.Event()
            instances.append(self)

        def initialize(self):
            return {"protocolVersion": 1}

        def _rpc(self, method, params):
            assert method == "session/new"
            assert params["mcpServers"][0]["args"][0] == "-I"
            self.session = "owned-session-" + str(len(instances))
            self.process = {"pid": 3333 + len(instances), "pgid": self.pid, "startticks": 1}
            self.run = self.cwd.parent
            directory = native.mcp.RunDirectory(self.run)
            try:
                directory.write_exclusive("process.json", self.process)
                start = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
                ready = {"jsonrpc": "2.0", "method": "notifications/initialized"}
                native.mcp._lifecycle(directory, {}, initialized=False, ready=False, reason="started")
                native.mcp._lifecycle(directory, start, initialized=True, ready=False,
                    reason="initialize_responded", protocol="2025-11-25")
                native.mcp._lifecycle(directory, ready, initialized=True, ready=True,
                    reason="initialized", protocol="2025-11-25")
            finally:
                directory.close()
            if flags["mode"] == "policy_change":
                flags["policy_changed"] = True
            model = "wrong" if flags["mode"] == "wrong_model" else "native-max"
            return {"sessionId": self.session, "models": {"currentModelId": model}}

        def drain_notifications(self):
            notices, self.notices = self.notices, []
            return notices

        def prompt(self, session, context):
            assert native.mcp.read_lifecycle(self.run)["ready"]
            self.prompted = True
            contexts.append(json.loads(context.split("\n", 1)[1]))
            if flags["mode"] == "cancel":
                cancelled.set()
                self.stopped.wait(1)
                return {"stopReason": "cancelled"}
            if flags["mode"] == "revoke":
                flags["revoked"] = True
                self.stopped.wait(1)
                return {"stopReason": "cancelled"}
            if flags["mode"] == "error":
                raise native.acp.ACPError("PRIVATE provider error", "unsupported_method")
            if flags["mode"] == "final":
                self.notices.append({"sessionId": session, "update": {
                    "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "done"}}})
                return {"stopReason": "end_turn"}
            directory = native.mcp.RunDirectory(self.run)
            try:
                config_value = directory.read("config.json")
                bound = directory.read("session.json")
                catalog = native.tools.ToolCatalog(config_value["tools"])
                handoff = native.tools.ToolHandoff(catalog, binding=bound["binding"],
                    session_id=session, server_id=bound["server_id"])
                name = config_value["tools"][0]["name"]
                tool = handoff.begin({"jsonrpc": "2.0", "id": "native-call", "method": "tools/call",
                    "params": {"name": name, "arguments": {"task": "client-only"}}},
                    binding=bound["binding"], session_id=session, server_id=bound["server_id"],
                    remaining_seconds=1)
                call = {"version": 1, "frontend_tool_use": tool.as_tool_use(),
                        "native_rpc_id": "native-call", "context": bound, "process": self.process}
                if flags["mode"] == "foreign_call":
                    call["context"]["binding"] = "foreign"
                directory.write_exclusive("call.json", call)
            finally:
                directory.close()
            return {"stopReason": "cancelled"}

        def close(self):
            self.closed = True
            self._process.returncode = 0
            self.stopped.set()

    monkeypatch.setattr(native, "_Controller", FakeController)
    monkeypatch.setattr(native.acp.subprocess, "Popen", lambda *_a, **_k: pytest.fail("real process forbidden"))
    cancelled = threading.Event()

    def run(request="request-0001", request_body=None, selected_config=None, selected_binding=None):
        return native.run_step(selected_config or config, selected_binding or binding(), request,
            body() if request_body is None else request_body,
            authorize=lambda _: not flags["revoked"], cancel_event=cancelled)

    return types.SimpleNamespace(run=run, config=config, instances=instances, flags=flags,
                                 cleanups=cleanups, contexts=contexts, state=state, cancel=cancelled)


@pytest.mark.parametrize("name", ["Read", "Edit", "Bash", "Write", "Grep", "Glob"])
def test_genuine_declared_tool_handoff_is_returned_only_after_cleanup(scenario, name):
    result = scenario.run(request_body=body(name))
    assert result.get("error_code") is None
    assert result["stop_reason"] == "tool_use" and result["content"][0]["name"] == name, result
    assert result["prompt_requests"] == 1 and result["native_cleanup_confirmed"]
    assert not result["raw_Messages_semantic_parity"]
    assert scenario.instances[0].closed and scenario.cleanups
    assert result["accounting"]["usage"] is None
    assert result["accounting"]["actual_provider_requests"] is None


def test_native_launch_uses_protected_proxy_without_upstream_secrets(
        scenario, protected, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient-secret@untrusted.invalid")
    monkeypatch.setenv("NO_PROXY", "*")
    result = scenario.run()
    assert result.get("error_code") is None and result["prompt_requests"] == 1
    environment = scenario.instances[0].options["env"]
    expected = native.egress.native_environment()
    assert all(environment.get(name) == value for name, value in expected.items())
    assert environment["HTTPS_PROXY"] == native.egress.PROXY_URL
    assert environment["NO_PROXY"] == environment["ALL_PROXY"] == ""
    assert protected.config["password"] not in json.dumps(environment)
    assert "ambient-secret" not in json.dumps(environment)


@pytest.mark.parametrize("broken", ["orphan_upstream", "missing_upstream", "invalid_policy"])
def test_native_incomplete_egress_never_launches_or_dispatches(scenario, protected, broken):
    if broken == "orphan_upstream":
        protected.policy.unlink()
    elif broken == "missing_upstream":
        protected.upstream.unlink()
    else:
        protected.policy.write_text('{"version":1,"enabled":false}')
    result = scenario.run()
    assert result["error_code"] == "native_egress_unavailable"
    assert result["prompt_requests"] == 0 and scenario.instances == []
    assert result["native_cleanup_confirmed"]


def test_native_unconfigured_route_stays_closed_to_ambient_proxy(scenario, protected, monkeypatch):
    protected.policy.unlink()
    protected.upstream.unlink()
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient-secret@untrusted.invalid")
    assert scenario.run().get("error_code") is None
    assert not native.egress.protected_native_environment()
    environment = scenario.instances[0].options["env"]
    assert not {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"} & environment.keys()


def test_continuation_uses_fresh_native_and_preserves_client_result_as_labeled_context(scenario):
    first = scenario.run(request_body=body("Bash"))
    continuation = body("Bash")
    continuation["messages"] += [{"role": "assistant", "content": first["content"]},
        {"role": "user", "content": [{"type": "tool_result",
            "tool_use_id": first["content"][0]["id"], "content": "real local result"}]}]
    scenario.flags["mode"] = "final"
    second = scenario.run("request-0002", continuation)
    assert second["stop_reason"] == "end_turn" and second["content"] == [{"type": "text", "text": "done"}]
    assert len(scenario.instances) == 2
    assert scenario.instances[0].session != scenario.instances[1].session
    assert scenario.contexts[-1] == continuation


def test_default_disabled_and_stale_ownership_never_launch_native(scenario):
    value = scenario.config.data
    value["enabled"] = False
    disabled = replace(scenario.config, _json=json.dumps(value))
    with pytest.raises(native.NativeError, match="disabled"):
        scenario.run(selected_config=disabled)
    with pytest.raises(native.NativeError, match="binding_changed"):
        scenario.run(selected_binding=replace(binding(), key_generation=2))
    assert scenario.instances == []


@pytest.mark.parametrize("mode", ["wrong_model", "policy_change"])
def test_model_or_policy_change_rejects_before_prompt(scenario, mode):
    scenario.flags["mode"] = mode
    result = scenario.run()
    assert result["prompt_requests"] == 0 and result["content"] == []
    assert all(not instance.prompted and instance.closed for instance in scenario.instances)


def test_missing_handshake_never_prompts(scenario, monkeypatch):
    monkeypatch.setattr(native, "_wait_ready", lambda *_: (_ for _ in ()).throw(
        native.NativeError("mcp_handshake_timeout")))
    result = scenario.run()
    assert result["prompt_requests"] == 0 and result["error_code"] == "mcp_handshake_timeout"
    assert scenario.instances[0].closed


def test_short_partial_lifecycle_append_after_ready_is_retried_before_delivery(scenario, monkeypatch):
    actual, partial_reads = native._lifecycle, []

    def lifecycle(run):
        value = actual(run)
        if scenario.instances[0].prompted and len(partial_reads) < 2:
            partial_reads.append(True)
            return {**value, "phase": "partial", "ready": False}
        return value

    monkeypatch.setattr(native, "_lifecycle", lifecycle)
    result = scenario.run()
    assert result["stop_reason"] == "tool_use" and result["native_cleanup_confirmed"]
    assert len(partial_reads) == 2


def test_persistent_partial_append_is_bounded_and_cannot_deliver_final_text(scenario, monkeypatch):
    scenario.flags["mode"] = "final"
    actual = native._lifecycle

    def lifecycle(run):
        value = actual(run)
        return {**value, "phase": "partial", "ready": False} if scenario.instances[0].prompted else value

    monkeypatch.setattr(native, "MCP_PARTIAL_GRACE_S", 0.04)
    monkeypatch.setattr(native, "_lifecycle", lifecycle)
    result = scenario.run()
    assert result["error_code"] == "mcp_lifecycle_partial_timeout"
    assert result["content"] == [] and result["native_cleanup_confirmed"]


@pytest.mark.parametrize("mode", ["cancel", "revoke", "error", "foreign_call"])
def test_failure_cancellation_or_foreign_call_never_delivers_frontend_action(scenario, mode):
    scenario.flags["mode"] = mode
    result = scenario.run()
    assert result["content"] == [] and result["stop_reason"] is None
    assert result["native_cleanup_confirmed"] and scenario.instances[0].closed
    assert "PRIVATE" not in json.dumps(result)


def test_unconfirmed_cleanup_suppresses_even_genuine_tool_use(scenario):
    scenario.flags["cleanup"] = False
    result = scenario.run()
    assert result["content"] == [] and result["error_code"] == "native_cleanup_unconfirmed"


def test_reserved_request_cannot_relaunch_or_overwrite_old_result(scenario):
    first = scenario.run()
    [run] = scenario.state.glob("step-*")
    saved = (run / "result.json").read_bytes()
    repeated = scenario.run()
    assert repeated["error_code"] == "request_already_reserved" and len(scenario.instances) == 1
    assert (run / "result.json").read_bytes() == saved
    assert first["stop_reason"] == "tool_use"


def test_busy_state_never_launches_or_reserves_native_work(scenario, monkeypatch):
    monkeypatch.setattr(native.fcntl, "flock", lambda *_: (_ for _ in ()).throw(BlockingIOError()))
    result = scenario.run()
    assert result["error_code"] == "native_busy" and result["prompt_requests"] == 0
    assert scenario.instances == [] and list(scenario.state.glob("step-*")) == []


def test_result_persistence_failure_never_exposes_completed_tool_action(scenario, monkeypatch):
    original = native.mcp.RunDirectory.write_exclusive

    def write(directory, name, value):
        if name == "result.json":
            raise OSError("PRIVATE_PATH")
        return original(directory, name, value)

    monkeypatch.setattr(native.mcp.RunDirectory, "write_exclusive", write)
    result = scenario.run()
    assert result["error_code"] == "result_persistence_failed"
    assert result["prompt_requests"] == 1 and result["native_cleanup_confirmed"]
    assert result["content"] == [] and "PRIVATE" not in json.dumps(result)


def test_private_file_reader_rejects_permissions_symlinks_and_hardlinks(tmp_path):
    path = tmp_path / "file"
    path.write_bytes(b"private")
    path.chmod(0o600)
    assert native._read(path, uid=os.getuid(), private=True) == b"private"
    path.chmod(0o644)
    with pytest.raises(native.NativeError):
        native._read(path, uid=os.getuid(), private=True)
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        native._read(link, uid=os.getuid(), private=True)
    link.unlink()
    os.link(path, link)
    with pytest.raises(native.NativeError):
        native._read(path, uid=os.getuid(), private=True)


def test_policy_snapshot_cannot_override_root_policy_enabled_or_binding(monkeypatch):
    original = b'{"enabled":false}'
    config = native.NativeConfig(Path("/private-policy"), hashlib.sha256(original).hexdigest(),
                                  '{"enabled":true}')
    monkeypatch.setattr(native, "_read", lambda *_a, **_k: original)
    with pytest.raises(native.NativeError, match="policy_changed"):
        config.validate()


@pytest.fixture
def policy(tmp_path, monkeypatch):
    """Exercise real policy parsing; fake only root ownership and immutable install pins."""
    home, state = tmp_path / "home", tmp_path / "state"
    home.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    (home / ".config/devin").mkdir(parents=True)
    native_config = home / ".config/devin/config.json"
    native_mcp = home / ".config/devin/mcp_config.json"
    settings = {"version": 1, "auto_update": False, "subagents_enabled": False,
                "agent": {"model": "native-max"}, "disabled_tools": sorted(native._DISABLED),
                "read_config_from": dict.fromkeys(native._IMPORTS, False),
                "permissions": {"allow": ["mcp__ccfleet__Read"], "deny": sorted(native._DENIED)},
                "hooks": {"PreToolUse": [{"matcher": "", "hooks": [{"type": "command",
                    "command": shlex.join(["/immutable/python", "-I", "/immutable/mcp", "--hook"]),
                    "timeout": 5}]}]}}
    files = {name: {"path": "/immutable/" + name, "sha256": "a" * 64} for name in (
        "executable", "python", "adapter", "acp", "mcp", "tools", "relay", "inference_policy", "egress",
        "package_init", "native_config", "native_mcp_config")}
    files["native_config"]["path"] = str(native_config)
    files["native_mcp_config"]["path"] = str(native_mcp)
    native_mcp.write_text('{"mcpServers":{}}')
    native_mcp.chmod(0o600)
    path = tmp_path / "policy.json"
    value = {"version": 1, "enabled": False, "execution_uid": os.geteuid(), "binding": asdict(binding()),
             "model_uid": "native-max", "verified_models": ["native-max"],
             "request_models": ["opus"], "model_state_values": ["native-max"], "thought_level": "max",
             "state_dir": str(state), "home": str(home), "files": files, "timeout_s": 60}
    actual_read = native._read

    def read(file, *, uid, **kwargs):
        return actual_read(file, uid=os.geteuid() if Path(file) == path else uid, **kwargs)

    monkeypatch.setattr(native, "_read", read)
    monkeypatch.setattr(native, "_root_ancestry", lambda _: None)
    monkeypatch.setattr(native, "_pinned", lambda record, **_: Path(record["path"]))
    monkeypatch.setattr(native.os, "access", lambda *_: True)

    def load():
        native_config.write_text(json.dumps(settings))
        native_config.chmod(0o600)
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return native.NativeConfig.load(path)

    return types.SimpleNamespace(load=load, value=value, settings=settings, path=path,
                                 home=home, state=state, native_mcp=native_mcp)


def test_valid_disabled_root_policy_loads_without_launch_and_can_be_revalidated(policy):
    config = policy.load()
    assert config.data["enabled"] is False
    assert config.validate() == (policy.home, policy.state)
    assert native.OwnershipBinding.from_dict(config.data["binding"]) == binding()
    status = config.status(binding())
    assert status["configured"] and not status["enabled"] and not status["mcp_ready"]
    assert status["model_requests"] == 0
    assert not config.status(replace(binding(), key_generation=2))["configured"]


def test_native_policy_requires_root_pinned_egress_module(policy):
    policy.value["files"].pop("egress")
    with pytest.raises(native.NativeError, match="invalid_source_manifest"):
        policy.load()


def test_native_policy_accepts_bounded_120_second_generation_without_launch(policy):
    policy.value["timeout_s"] = 120
    config = policy.load()
    assert config.data["timeout_s"] == 120
    assert config.status(binding())["model_requests"] == 0


@pytest.mark.parametrize("change", [
    {"enabled": 1}, {"execution_uid": 0}, {"request_models": "opus"},
    {"verified_models": ["other"]}, {"thought_level": "unknown"}, {"timeout_s": 121},
    {"version": True}, {"extra": "unsupported"}])
def test_malformed_policy_has_no_authority(policy, change):
    policy.value.update(change)
    with pytest.raises(native.NativeError):
        policy.load()


@pytest.mark.parametrize("field,change", [
    ("version", None), ("version", True), ("version", 0), ("version", 2),
    ("auto_update", True), ("subagents_enabled", True), ("disabled_tools", []),
    ("read_config_from", {}), ("hooks", {}), ("permissions", {"allow": ["*"], "deny": []}),
    ("agent", {"model": "other"})])
def test_native_builtin_import_or_hook_containment_cannot_be_omitted(policy, field, change):
    policy.settings[field] = change
    with pytest.raises(native.NativeError, match="native_containment_guard"):
        policy.load()


def test_unversioned_native_settings_reject_before_startup_can_rewrite_pinned_bytes(policy):
    policy.settings.pop("version")
    with pytest.raises(native.NativeError, match="native_containment_guard"):
        policy.load()


def test_exact_catalog_scoped_ccfleet_mcp_wildcard_is_permitted_in_root_profile(policy):
    policy.settings["permissions"]["allow"] = ["mcp__ccfleet__*"]
    assert policy.load().data["enabled"] is False


@pytest.mark.parametrize("rule", ["*", "mcp__*", "mcp__other__*", "mcp__ccfleet*",
                                  "mcp__ccfleet__Read*", "mcp__ccfleet__*Extra", "read*", "Bash(*)"])
def test_other_native_or_mcp_wildcards_remain_forbidden(policy, rule):
    policy.settings["permissions"]["allow"] = [rule]
    with pytest.raises(native.NativeError, match="native_containment_guard"):
        policy.load()


def test_root_policy_changes_and_existing_extra_mcp_servers_are_rejected(policy):
    config = policy.load()
    policy.path.write_text(policy.path.read_text() + " ")
    with pytest.raises(native.NativeError, match="policy_changed"):
        config.validate()
    policy.native_mcp.write_text('{"mcpServers":{"other":{}}}')
    with pytest.raises(native.NativeError, match="native_mcp_config_guard"):
        policy.load()


def test_source_pins_reject_hash_changes_and_writable_source_ancestry(tmp_path):
    path = tmp_path / "source"
    path.write_bytes(b"reviewed")
    path.chmod(0o600)
    record = {"path": str(path), "sha256": hashlib.sha256(b"reviewed").hexdigest()}
    assert native._pinned(record, uid=os.geteuid(), private=True) == path
    path.write_bytes(b"changed")
    with pytest.raises(native.NativeError, match="source_digest_guard"):
        native._pinned(record, uid=os.geteuid(), private=True)
    with pytest.raises(native.NativeError, match="source_directory_guard"):
        native._root_ancestry(path)


def test_root_traverse_only_ancestry_uses_opath_without_listing(monkeypatch):
    opened, closed = [], []
    monkeypatch.setattr(native.os, "O_PATH", 0x200000, raising=False)

    def open_directory(path, flags, **kwargs):
        opened.append((path, flags, kwargs))
        return len(opened) + 100

    monkeypatch.setattr(native.os, "open", open_directory)
    monkeypatch.setattr(native.os, "close", closed.append)
    monkeypatch.setattr(native.os, "fstat", lambda _: types.SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o711, st_uid=0))
    native._root_ancestry(Path("/etc/ccfleet/devin/slot/key.json"))
    assert len(opened) == len(closed) == 5
    assert all(flags & 0x200000 and flags & os.O_DIRECTORY and flags & os.O_NOFOLLOW
               for _, flags, _ in opened)


def test_empty_final_non_success_stops_or_unexpected_tools_are_never_final_text():
    for answer, notices in (({"stopReason": "cancelled"}, []), ({"stopReason": "end_turn"}, []),
                            ({"stopReason": "end_turn"}, [{"sessionId": "session", "update": {
                                "sessionUpdate": "tool_call"}}])):
        with pytest.raises(native.NativeError):
            native._final_text(notices, "session", answer)


def test_accounting_is_unknown_when_database_absent_and_never_zero_usage(tmp_path):
    value = native._accounting(tmp_path, "owned-session", "native-max")
    assert value["usage"] is None and value["unique_finalized_provider_ids"] is None
    assert value["actual_provider_requests"] is None


def test_finalized_database_counts_are_deduplicated_and_unknown_cache_fields_stay_unknown(tmp_path):
    folder = tmp_path / ".local/share/devin/cli"
    folder.mkdir(parents=True)
    path = folder / "sessions.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE message_nodes(session_id TEXT,chat_message TEXT)")
        for request in ("same", "same", "other"):
            db.execute("INSERT INTO message_nodes VALUES (?,?)", ("session", json.dumps({
                "role": "assistant", "metadata": {"request_id": request, "generation_model": "native-max",
                    "metrics": {"input_tokens": 2, "output_tokens": 3}}})))
    path.chmod(0o600)
    value = native._accounting(tmp_path, "session", "native-max")
    assert value["unique_finalized_provider_ids"] == 2
    assert value["actual_provider_requests"] is None
    assert value["usage"] == {"input_tokens": 4, "output_tokens": 6,
                              "cache_read_tokens": None, "cache_creation_tokens": None}
    assert value["served_model_verified"]
    assert value["served_model_evidence"] == "finalized_native_DB_generation_model"


@pytest.mark.parametrize("metadata,expected_count,mismatch", [
    (None, 0, False), ({"generation_model": "native-max"}, 0, False),
    ({"request_id": "partial"}, 0, False),
    ({"request_id": "final", "generation_model": "other-model"}, None, True)])
def test_zero_partial_or_mismatched_database_metadata_never_verifies_served_model(
        tmp_path, metadata, expected_count, mismatch):
    folder = tmp_path / ".local/share/devin/cli"
    folder.mkdir(parents=True)
    path = folder / "sessions.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE message_nodes(session_id TEXT,chat_message TEXT)")
        if metadata is not None:
            db.execute("INSERT INTO message_nodes VALUES (?,?)", (
                "session", json.dumps({"role": "assistant", "metadata": metadata})))
    path.chmod(0o600)
    value = native._accounting(tmp_path, "session", "native-max")
    assert value["served_model_verified"] is False
    assert value["unique_finalized_provider_ids"] == expected_count
    assert value["model_mismatch_detected"] is mismatch
    assert value["actual_provider_requests"] is None and value["usage"] is None


def test_result_distinguishes_configured_model_from_database_verified_model(scenario, monkeypatch):
    monkeypatch.setattr(native, "_accounting", lambda *_: {"usage": None,
        "unique_finalized_provider_ids": 1, "actual_provider_requests": None,
        "served_model_verified": True, "model_mismatch_detected": False})
    result = scenario.run()
    assert result["model"] == result["verified_model"] == "native-max"


@pytest.mark.parametrize("mode", ["private_home_644_db", "public_home", "writable_db", "symlink_db"])
def test_native_644_database_requires_private_same_owner_home_and_readonly_safe_path(tmp_path, mode):
    home = tmp_path / "native-home"
    home.mkdir(mode=0o700)
    folder = home / ".local/share/devin/cli"
    folder.mkdir(parents=True)
    path = folder / "sessions.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE message_nodes(session_id TEXT,chat_message TEXT)")
        db.execute("INSERT INTO message_nodes VALUES (?,?)", ("session", json.dumps({
            "role": "assistant", "metadata": {"request_id": "final-id", "generation_model": "native-max",
                "metrics": {"input_tokens": 2, "output_tokens": 3}}})))
    path.chmod(0o644)
    if mode == "public_home":
        home.chmod(0o755)
    elif mode == "writable_db":
        path.chmod(0o666)
    elif mode == "symlink_db":
        target = path.with_name("other.db")
        path.rename(target)
        path.symlink_to(target)
    value = native._accounting(home, "session", "native-max")
    assert value["served_model_verified"] is (mode == "private_home_644_db")
    if mode == "private_home_644_db":
        assert value["usage"]["input_tokens"] == 2
        assert value["unique_finalized_provider_ids"] == 1
    else:
        assert value["usage"] is None and value["unique_finalized_provider_ids"] is None


def test_cli_default_plan_does_not_read_config_or_launch(monkeypatch, capsys):
    monkeypatch.setattr(native.sys, "argv", ["devin_native", "--config", "/must-not-read"])
    monkeypatch.setattr(native.NativeConfig, "load", lambda *_: pytest.fail("no policy read"))
    monkeypatch.setattr(native, "run_step", lambda *_a, **_k: pytest.fail("no execution"))
    assert native.main() == 0
    assert json.loads(capsys.readouterr().out)["model_requests"] == 0
