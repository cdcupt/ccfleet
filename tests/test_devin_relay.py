"""Fake wire/policy/runner tests; no native, SSH, account or provider calls."""

from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import types
from dataclasses import asdict, replace

import pytest

from ccfleet_agent import devin_relay as relay
from tests.test_devin_native import binding, body


def context():
    model_map = {"model_uid": "native-max", "request_models": ["opus"], "verified_models": ["native-max"]}
    return {"binding": asdict(binding()), "runtime": {"bridge_contract": "native-agent-v1",
        "native_version": "3000.11.92", "adapter_revision": "a" * 64,
        "model_map_digest": hashlib.sha256(relay._json(model_map)).hexdigest(),
        "provider_account_fp": "c" * 64}, "request_id": "d" * 32}


def frame(value):
    raw = json.dumps({"version": 2, **value}).encode()
    return struct.pack("!I", len(raw)) + raw


def request(value=None, *, path="/v1/messages", native=None):
    raw = json.dumps(body() if value is None else value).encode()
    return frame({"operation": "request", "method": "POST", "path": path,
                  "headers": {"content-type": "application/json", "anthropic-version": "2023-06-01"},
                  "body_size": len(raw), "native": context() if native is None else native}) + raw


def responses(raw):
    stream, result = io.BytesIO(raw), []
    while stream.tell() < len(raw):
        length, = struct.unpack("!I", stream.read(4))
        meta = json.loads(stream.read(length))
        chunks = bytearray()
        while True:
            size, = struct.unpack("!I", stream.read(4))
            if not size:
                break
            assert size <= relay.MAX_CHUNK
            chunks.extend(stream.read(size))
        result.append((meta, bytes(chunks)))
    return result


@pytest.fixture
def scenario():
    cfg = types.SimpleNamespace(data={"model_uid": "native-max", "request_models": ["opus"],
                                     "thought_level": "max"})
    state = {"allow": True, "called": 0, "result": None, "after": None}
    policy = relay.PolicySession(cfg, binding(), relay._json(context()["runtime"]).decode(),
                                 lambda: state["allow"])

    def loader(value):
        assert value == context()
        return policy

    def runner(config, current, request_id, clean, *, authorize, cancel_event):
        assert config is cfg and current == binding() and request_id == "d" * 32
        assert authorize(current) is True and not cancel_event.is_set()
        assert "metadata" not in clean
        state["called"] += 1
        if state["after"]:
            state["after"]()
        return state["result"] or {"mode": "native_agent_compatibility", "contract": "native-agent-v1",
            "prompt_requests": 1, "native_cleanup_confirmed": True, "raw_Messages_semantic_parity": False,
            "model": "native-max", "verified_model": None, "accounting": {"usage": None},
            "content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"}

    def run(extra=b"", *, input_bytes=None, selected_loader=None, selected_runner=None):
        output = io.BytesIO()
        initial = frame({"operation": "status", "native": context()})
        code = relay.serve(io.BytesIO(initial + extra if input_bytes is None else input_bytes), output,
                           policy_loader=selected_loader or loader, runner=selected_runner or runner)
        return code, responses(output.getvalue())

    return types.SimpleNamespace(run=run, state=state, policy=policy, loader=loader, runner=runner)


def test_status_is_exact_context_bound_and_model_free(scenario):
    code, [(meta, raw)] = scenario.run()
    assert code == 0 and scenario.state["called"] == 0
    assert meta == {"version": 2, "status": 200, "headers": {"content-type": "application/json"},
                    "retryable": False}
    assert json.loads(raw) == {"ready": True, "protocol": 2, "provider": "devin",
        "bridge_contract": "native-agent-v1", "binding_digest": binding().digest}


@pytest.mark.parametrize("stream", [True, False])
def test_text_response_preserves_explicit_unknown_usage_without_zero_fill(scenario, stream):
    value = body()
    value["stream"] = stream
    value["metadata"] = {"user_id": "PRIVATE"}
    code, replies = scenario.run(request(value))
    assert code == 0 and scenario.state["called"] == 1
    meta, raw = replies[1]
    assert meta["status"] == 200 and not meta["retryable"]
    if stream:
        events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith(b"data: ")]
        assert events[0]["message"]["usage"] is None
        assert next(event for event in events if event["type"] == "message_delta")["usage"] is None
        assert events[-1]["type"] == "message_stop"
    else:
        assert json.loads(raw)["usage"] is None
    assert b'"input_tokens":0' not in raw and b"PRIVATE" not in raw


def test_genuine_client_tool_response_sse_is_framed_and_carries_exact_arguments(scenario):
    scenario.state["result"] = {"mode": "native_agent_compatibility", "contract": "native-agent-v1",
        "prompt_requests": 1, "native_cleanup_confirmed": True, "raw_Messages_semantic_parity": False,
        "model": "native-max", "accounting": {"usage": {"input_tokens": 3, "output_tokens": 5,
            "cache_read_tokens": None, "cache_creation_tokens": 7}},
        "content": [{"type": "tool_use", "id": "toolu_devin_mcp_" + "a" * 32,
                     "name": "Read", "input": {"file_path": "/client/only"}}], "stop_reason": "tool_use"}
    code, replies = scenario.run(request())
    assert code == 0
    events = [json.loads(line[6:]) for line in replies[1][1].splitlines() if line.startswith(b"data: ")]
    delta = next(event["delta"] for event in events if event["type"] == "content_block_delta")
    assert delta == {"type": "input_json_delta", "partial_json": '{"file_path":"/client/only"}'}
    usage = events[0]["message"]["usage"]
    assert usage == {"input_tokens": 3, "output_tokens": 5, "cache_creation_input_tokens": 7}
    assert "cache_read_input_tokens" not in usage


def test_count_tokens_is_501_without_running_or_forwarding(scenario):
    code, replies = scenario.run(request(path="/v1/messages/count_tokens"))
    assert code == 2 and replies[1][0]["status"] == 501
    assert scenario.state["called"] == 0


@pytest.mark.parametrize("change", [
    {"request_id": "other"}, {"request_id": "e" * 32},
    {"binding": asdict(replace(binding(), key_generation=2))},
    {"runtime": {**context()["runtime"], "provider_account_fp": "e" * 64}}])
def test_context_cannot_change_between_status_and_request(scenario, change):
    code, replies = scenario.run(request(native={**context(), **change}))
    assert code == 2 and replies[1][0]["status"] >= 400
    assert scenario.state["called"] == 0


def test_revocation_after_native_completion_suppresses_every_success_byte(scenario):
    scenario.state["after"] = lambda: scenario.state.update(allow=False)
    code, replies = scenario.run(request())
    assert code == 2 and replies[1][0]["status"] == 403
    assert b"done" not in replies[1][1] and not replies[1][0]["retryable"]


def test_input_disconnect_before_launch_never_runs_adapter(scenario, monkeypatch):
    monkeypatch.setattr(relay._Input, "disconnected", lambda _: True)
    code, replies = scenario.run(request())
    assert code == 2 and replies[1][0]["status"] == 403
    assert scenario.state["called"] == 0


def test_signal_cancel_before_any_frame_never_loads_policy_or_runs_adapter(scenario):
    cancelled = threading_event()
    cancelled.set()
    output = io.BytesIO()
    code = relay.serve(io.BytesIO(), output, cancelled=cancelled,
                       policy_loader=lambda _: pytest.fail("no policy after cancel"),
                       runner=lambda *_a, **_k: pytest.fail("no native after cancel"))
    assert code == 2 and responses(output.getvalue())[0][0]["status"] == 403


def test_wrong_runtime_or_binding_from_loader_never_becomes_ready(scenario):
    for policy in (replace(scenario.policy, binding=replace(binding(), owner_id="other")),
                   replace(scenario.policy, runtime_json="{}")):
        code, replies = scenario.run(selected_loader=lambda _, policy=policy: policy)
        assert code == 2 and replies[0][0]["status"] == 403
    assert scenario.state["called"] == 0


@pytest.mark.parametrize("bad", [
    {"error_code": "PRIVATE_PROVIDER_TEXT"}, {"native_cleanup_confirmed": False},
    {"model": "other"}, {"verified_model": "other"}, {"prompt_requests": 0},
    {"content": [{"type": "image", "source": {}}]}, {"stop_reason": "cancelled"},
    {"accounting": {"usage": {"input_tokens": True, "output_tokens": 1}}}])
def test_invalid_model_cleanup_content_or_usage_never_delivers_response(scenario, bad):
    good = scenario.runner(scenario.policy.config, binding(), "d" * 32, body(),
                           authorize=lambda _: True, cancel_event=threading_event())
    scenario.state["called"] = 0
    scenario.state["result"] = {**good, **bad}
    code, replies = scenario.run(request())
    assert code == 2 and replies[1][0]["status"] >= 400
    assert b"PRIVATE" not in replies[1][1] and not replies[1][0]["retryable"]


def threading_event():
    import threading
    return threading.Event()


def test_unsupported_rich_content_rejects_before_runner(scenario):
    value = body()
    value["messages"][0]["content"] = [{"type": "image", "source": {}}]
    code, replies = scenario.run(request(value))
    assert code == 2 and replies[1][0]["status"] == 400
    assert scenario.state["called"] == 0


def test_framing_duplicate_keys_partial_and_oversized_metadata_fail_closed(scenario):
    duplicate = b'{"version":2,"version":2,"operation":"status"}'
    for data in (b"\0\0", struct.pack("!I", relay.MAX_META + 1),
                 struct.pack("!I", len(duplicate)) + duplicate):
        code, replies = scenario.run(input_bytes=data)
        assert code == 2 and replies[0][0]["status"] == 400
    assert scenario.state["called"] == 0


def test_input_wait_observes_cancellation_without_an_extra_thread(monkeypatch):
    cancelled = threading_event()
    stream = types.SimpleNamespace(fileno=lambda: 999)

    def selected(*_):
        cancelled.set()
        return [], [], []

    monkeypatch.setattr(relay.select, "select", selected)
    monkeypatch.setattr(relay.os, "read", lambda *_: pytest.fail("no descriptor read"))
    with pytest.raises(relay.WireError, match="native_cancelled"):
        relay._Input(stream, cancelled).read(4)


@pytest.fixture
def root_policy(tmp_path, monkeypatch):
    """Root file ownership is simulated; grant/profile/credential content checks are real."""
    user = "slot01"
    parent = tmp_path / user
    parent.mkdir()
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    credential = home / ".local/share/devin/credentials.toml"
    credential.parent.mkdir(parents=True)
    credential.write_bytes(b"FAKE private native credential")
    credential.chmod(0o600)
    stem = hashlib.sha256(binding().key_id.encode()).hexdigest()
    profile = parent / (stem + ".json")
    profile.write_bytes(b"root-pinned-profile")
    grant_path = parent / (stem + ".grant.json")
    grant = {"version": 1, "enabled": True, "identity_verified": True,
             "binding": context()["binding"], "runtime": context()["runtime"],
             "config_path": str(profile), "config_sha256": hashlib.sha256(profile.read_bytes()).hexdigest(),
             "credential_sha256": hashlib.sha256(credential.read_bytes()).hexdigest(), "observed_at": 1000}
    config = types.SimpleNamespace(data={"home": str(home), "model_uid": "native-max",
        "request_models": ["opus"], "verified_models": ["native-max"],
        "files": {"adapter": {"sha256": "a" * 64}}},
        policy_sha256=grant["config_sha256"], status=lambda _: {"configured": True, "enabled": True})
    actual_read = relay.native._read
    actual_uid = os.geteuid()

    def read(path, *, uid, **kwargs):
        return actual_read(path, uid=actual_uid, **kwargs)

    monkeypatch.setattr(relay, "POLICY_ROOT", tmp_path)
    monkeypatch.setattr(relay.os, "geteuid", lambda: actual_uid or 1001)
    monkeypatch.setattr(relay.pwd, "getpwuid", lambda _: types.SimpleNamespace(pw_name=user))
    monkeypatch.setattr(relay.native, "_root_ancestry", lambda _: None)
    monkeypatch.setattr(relay.native, "_read", read)
    def load_config(path, *, expected_sha256):
        if path != profile or expected_sha256 != config.policy_sha256:
            raise relay.native.NativeError("policy_digest_guard")
        return config

    monkeypatch.setattr(relay.native.NativeConfig, "load", load_config)

    def load():
        grant_path.write_text(json.dumps(grant))
        grant_path.chmod(0o640)
        return relay.load_policy(context(), now=lambda: 1001)

    return types.SimpleNamespace(load=load, grant=grant, grant_path=grant_path, profile=profile,
                                 credential=credential, config=config)


def test_fresh_root_grant_binds_profile_and_private_native_credential(root_policy):
    loaded = root_policy.load()
    assert loaded.authorize(binding())
    assert not loaded.authorize(replace(binding(), key_generation=2))
    root_policy.credential.write_bytes(b"changed native login")
    assert not loaded.authorize(binding())


@pytest.mark.parametrize("change", [
    {"enabled": False}, {"enabled": 1}, {"identity_verified": False},
    {"observed_at": 700}, {"observed_at": 1002}, {"observed_at": True},
    {"config_sha256": "e" * 64}, {"credential_sha256": "e" * 64},
    {"config_path": "/different/profile"}, {"binding": asdict(replace(binding(), owner_id="other"))},
    {"runtime": {**context()["runtime"], "provider_account_fp": "e" * 64}}, {"extra": True}])
def test_missing_stale_or_foreign_grant_never_authorizes(root_policy, change):
    root_policy.grant.update(change)
    with pytest.raises((relay.WireError, relay.native.NativeError)):
        root_policy.load()


def test_grant_removal_profile_change_or_grant_revocation_invalidates_live_session(root_policy):
    loaded = root_policy.load()
    root_policy.grant["enabled"] = False
    root_policy.grant_path.write_text(json.dumps(root_policy.grant))
    assert not loaded.authorize(binding())


def test_root_profile_replacement_and_runtime_map_drift_invalidate_grant(root_policy):
    loaded = root_policy.load()
    root_policy.profile.write_bytes(b"different root policy")
    assert not loaded.authorize(binding())
    root_policy.profile.write_bytes(b"root-pinned-profile")
    root_policy.config.data["files"]["adapter"]["sha256"] = "f" * 64
    with pytest.raises(relay.WireError, match="native_runtime_profile_mismatch"):
        root_policy.load()
    root_policy.grant_path.unlink()
    assert not loaded.authorize(binding())


def test_default_cli_plan_does_not_load_policy_or_read_frames(monkeypatch, capsys):
    monkeypatch.setattr(relay, "serve", lambda *_a, **_k: pytest.fail("no execution"))
    assert relay.main([]) == 0
    assert json.loads(capsys.readouterr().out)["model_requests"] == 0
