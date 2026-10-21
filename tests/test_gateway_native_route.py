"""Provider routing through local HTTP and fake pipes; no SSH/native/provider calls."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import struct
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ccfleetd import gateway_transport, native_preflight, slots, throughput
from ccfleetd.api import Context, build_server
from ccfleetd.gateway import GatewayRuntime
from ccfleetd.gateway_transport import GatewayTransportError, NativeSSHAttempt, SSHAttempt
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from tests.test_api import call
from tests.test_gateway_end_to_end import Attempt, Pool, request, wait_for
from tests.test_gateway_provider_controls import bind, native_key, native_report, native_targets
from tests.test_gateway_provider_controls import controls as controls
from tests.test_gateway_security import framed, reply, service_attempt


class NativeAttempt(Attempt):
    def check(self):
        return not self.plan.get("unavailable", False)


class NativePool(Pool):
    def __call__(self, target, _cfg):
        planned = self.plans[target["slot_id"]]
        attempt = NativeAttempt(self, copy.deepcopy(target), planned.popleft() if planned else {})
        self.attempts.append(attempt)
        return attempt


@pytest.fixture
def native_http(controls):
    c = controls
    c.store._conn.execute("UPDATE slots SET state=? WHERE id='s1'", (slots.CLAIMED,))
    c.store._conn.commit()
    bind(c)
    key = native_key(c)
    pool = NativePool()
    runtime = GatewayRuntime(c.cfg, c.store, transport_factory=pool)
    context = Context(c.store, c.cfg, Monitor(c.store, c.cfg, LogNotifier()))
    context.gateway.close()
    context.gateway = runtime
    server = build_server(context, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    c.key, c.pool, c.gateway, c.server = key, pool, runtime, server
    yield c
    for attempt in pool.attempts:
        attempt.close()
    server.shutdown()
    server.server_close()
    thread.join(2)
    runtime.close()


def test_native_http_enriches_only_server_copy_and_preserves_context(native_http):
    c = native_http
    original = native_targets(c, c.key)[0]
    body = {"model": "opus", "max_tokens": 40, "stream": True,
        "messages": [{"role": "user", "content": "Visible task"}],
        "tools": [{"name": "Read", "description": "Local read", "input_schema": {"type": "object"}}],
        "metadata": {"user_id": "PRIVATE_CALLER_ID"}}
    status, _, headers = request(c, body)
    assert status == 200 and headers["x-should-retry"] == "false"
    [attempt] = c.pool.attempts
    native = attempt.target["_native_context"]
    assert native["binding"] == {"owner_id": "a1", "key_id": c.key["id"],
        "key_generation": c.key["generation"], "slot_id": "s1",
        "claim_id": original["claim_id"], "claim_generation": original["claim_generation"],
        "provider_generation": 1, "provider": "devin"}
    assert native["runtime"] == {**c.runtime, "provider_account_fp": "a"*64}
    assert len(native["request_id"]) == 32 and int(native["request_id"], 16) >= 0
    assert native_targets(c, c.key)[0] == original
    forwarded = c.pool.requests[0]["body"]
    assert forwarded["messages"] == body["messages"] and forwarded["tools"] == body["tools"]
    assert "metadata" not in forwarded
    assert c.store.get_slot("s1")["state"] == slots.CLAIMED


@pytest.mark.parametrize("provider", ["devin", "anthropic"])
def test_stock_placeholder_annotation_is_native_only(native_http, provider):
    from tests.test_stock_native_request import stock_body

    c = native_http
    if provider == "anthropic":
        c.store.move_slot("s1", slots.ACTIVE)
        c.key = c.store.create_gateway_key("a1", "Anthropic endpoint", ["s1"], now=c.now)
    body = stock_body()
    status, _, _ = request(c, body)
    assert status == 200
    forwarded = c.pool.requests[0]["body"]
    assert ("defer_loading" in forwarded["tools"][0]) is (provider == "anthropic")
    assert forwarded["messages"] == body["messages"]
    assert forwarded["tools"][1] == body["tools"][1]


@pytest.fixture
def reviewed_native_http(native_http):
    c = native_http
    runtime = native_preflight._REVIEWED_RUNTIME
    c.cfg = replace(c.cfg, gateway_devin_native_version=runtime["native_version"],
                    gateway_devin_adapter_revision=runtime["adapter_revision"],
                    gateway_devin_model_map_digest=runtime["model_map_digest"])
    c.gateway.cfg = c.cfg
    c.runtime = dict(runtime)
    bind(c, generation=1, **runtime)
    c.key = native_key(c)
    return c


@pytest.mark.parametrize("change,code", [
    ({"model": "PRIVATE_UNSUPPORTED_MODEL"}, "unsupported_model"),
    ({"output_config": {"effort": "high"}}, "unsupported_output_control"),
    ({"thinking": {"type": "enabled", "budget_tokens": 32000}}, "unsupported_thinking_control"),
    ({"temperature": 1}, "unsupported_native_request"),
    ({"context_management": {"edits": [{"type": "unknown"}]}}, "unsupported_context_control"),
    ({"messages": [{"role": "assistant", "content": [{"type": "thinking",
        "thinking": "PRIVATE_THINKING", "signature": "PRIVATE_SIGNATURE", "extra": True}]}]},
     "unsupported_content"),
    ({"messages": [{"role": "user", "content": [{"type": "tool_result",
        "tool_use_id": "toolu_synthetic", "content": [{"type": "tool_reference",
        "tool_name": "PRIVATE_TOOL"}]}]}]}, "unsupported_content"),
    ({"messages": [{"role": [], "content": "PRIVATE_PROMPT"}]}, "invalid_messages_body"),
    ({"tools": [{"name": "Read", "description": "PRIVATE_TOOL", "defer_loading": True,
        "input_schema": {"type": "object"}}]}, "unsupported_tool_declaration"),
])
def test_reviewed_native_rejection_is_actionable_before_transport(
        reviewed_native_http, change, code, caplog):
    from tests.test_stock_native_request import stock_body

    c = reviewed_native_http
    body = {**stock_body(), **change}
    before = copy.deepcopy(body)
    with caplog.at_level("INFO"):
        status, raw, headers = request(c, body)
    error = json.loads(raw)["error"]
    assert status == 400 and error["type"] == "invalid_request_error" and error["code"] == code
    assert headers["x-should-retry"] == "false" and b"PRIVATE" not in raw
    assert body == before and c.pool.attempts == [] and c.gateway.active == 0
    assert [r.getMessage() for r in caplog.records if r.name == "ccfleetd.gateway"] == [
        "Devin request rejected before execution: code=" + code]
    assert "PRIVATE" not in caplog.text


def test_reviewed_context_limit_keeps_claude_recovery_signal_without_dispatch(reviewed_native_http):
    from ccfleet_agent import devin_native as native
    from ccfleetd.gateway_providers import native_client_request
    from tests.test_stock_native_request import stock_body

    c = reviewed_native_http
    body = stock_body()
    size = len(native.native_context(native_client_request(body)).encode())
    body["messages"][0]["content"] += "x" * (native.MAX_PROMPT - size + 1)
    status, raw, headers = request(c, body)
    error = json.loads(raw)["error"]
    assert status == 400 and error["type"] == "invalid_request_error"
    assert error["code"] == "native_context_limit" and error["message"].startswith("prompt is too long:")
    assert headers["x-should-retry"] == "false" and c.pool.attempts == []


@pytest.mark.parametrize("model", ["opus", "claude-opus-5-5"])
def test_reviewed_supported_request_still_dispatches_unchanged(reviewed_native_http, model):
    from ccfleetd.gateway_providers import native_client_request
    from tests.test_stock_native_request import stock_body

    c = reviewed_native_http
    body = {**stock_body(), "model": model}
    assert request(c, body)[0] == 200
    assert c.pool.requests[0]["body"] == native_client_request(body)


def test_unknown_native_profile_still_uses_existing_node_validation(native_http, monkeypatch):
    from tests.test_stock_native_request import stock_body

    c = native_http
    monkeypatch.setattr(native_preflight.native, "validate_native_request",
                        lambda *a, **k: pytest.fail("unknown profile preflight"))
    assert request(c, {**stock_body(), "temperature": 1})[0] == 200
    assert len(c.pool.attempts) == 1


def test_reviewed_thinking_history_forwards_visible_read_continuation(reviewed_native_http):
    from tests.test_stock_native_request import stock_body

    c = reviewed_native_http
    body = stock_body()
    visible = {"type": "tool_use", "id": "toolu_read", "name": "Read", "input": {"path": "visible"}}
    result = {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_read", "content": "Visible local result"}
    ]}
    body["messages"] += [
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "PRIVATE_REASONING", "signature": "PRIVATE_SIGNATURE"},
            visible,
        ]}, result,
    ]
    before = copy.deepcopy(body)
    assert request(c, body)[0] == 200 and body == before
    forwarded = c.pool.requests[0]["body"]
    assert forwarded["messages"] == [body["messages"][0],
                                       {"role": "assistant", "content": [visible]}, result]
    assert "PRIVATE" not in json.dumps(forwarded)


@pytest.mark.parametrize("provider", ["devin", "anthropic"])
def test_private_history_is_unchanged_for_unknown_profile_or_anthropic(native_http, provider):
    from tests.test_stock_native_request import stock_body

    c = native_http
    if provider == "anthropic":
        c.store.move_slot("s1", slots.ACTIVE)
        c.key = c.store.create_gateway_key("a1", "Anthropic endpoint", ["s1"], now=c.now)
    body = stock_body()
    history = {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "Synthetic private reasoning", "signature": "Synthetic"}
    ]}
    body["messages"].append(history)
    assert request(c, body)[0] == 200
    assert c.pool.requests[0]["body"]["messages"] == body["messages"]


def test_native_status_is_model_free_private_and_cache_bound_to_key_generation(native_http):
    c = native_http
    assert c.gateway.status(c.key)["ready"]
    assert c.gateway.status(c.key)["ready"]
    assert len(c.pool.attempts) == 1 and c.pool.requests == []
    assert "account_fp" not in repr(c.gateway.status(c.key))
    changed = c.store.update_gateway_key(c.key["id"], "a1", name="Renamed", slot_ids=["s1"],
        mode="sticky", provider="devin", allow_devin=True, provider_runtime=c.runtime, now=c.now)
    assert changed["generation"] != c.key["generation"]
    assert c.gateway.status(changed)["ready"]
    assert len(c.pool.attempts) == 2
    assert (c.pool.attempts[0].target["_native_context"]["request_id"]
            != c.pool.attempts[1].target["_native_context"]["request_id"])


@pytest.mark.parametrize("changes", [{"gateway_devin_enabled": False},
    {"gateway_devin_native_version": ""}, {"gateway_devin_adapter_revision": "f"*64}])
def test_disabled_or_unregistered_native_runtime_never_dispatches(native_http, changes):
    c = native_http
    c.gateway.cfg = replace(c.cfg, **changes)
    status, _, headers = request(c)
    assert status == 503 and headers["x-should-retry"] == "false"
    assert not c.gateway.status(c.key)["ready"] and c.pool.attempts == []


@pytest.mark.parametrize("path", ["/v1/messages/count_tokens", "/v1/messages/count_tokens?beta=true"])
def test_native_count_tokens_is_explicitly_unsupported_without_any_transport(native_http, path):
    c = native_http
    status, body, headers = request(c, path=path)
    assert status == 501 and b"Token counting" in body
    assert headers["x-should-retry"] == "false"
    assert c.pool.attempts == [] and c.gateway.active == 0


def test_native_beta_messages_http_post_keeps_path_and_body(native_http):
    c = native_http
    path = "/v1/messages?beta=true"
    body = {"model": "opus", "max_tokens": 32, "stream": True,
        "messages": [{"role": "user", "content": "Beta request"}]}
    status, _, headers = request(c, body, path=path)
    assert status == 200 and headers["x-should-retry"] == "false"
    assert len(c.pool.attempts) == len(c.pool.requests) == 1
    assert c.pool.requests[0]["path"] == path and c.pool.requests[0]["body"] == body


@pytest.mark.parametrize("path", ["/v1/messages", "/v1/messages?beta=true"])
@pytest.mark.parametrize("provider", ["devin", "anthropic"])
def test_buffered_native_sse_is_excluded_from_live_stream_throughput(
        native_http, monkeypatch, path, provider):
    c = native_http
    if provider == "anthropic":
        c.store.move_slot("s1", slots.ACTIVE)
        c.key = c.store.create_gateway_key("a1", "Anthropic endpoint", ["s1"], now=c.now)
    created, submitted = [], []
    sample = throughput.Sample(5000, 1000.0)
    def observer(media_type, **kwargs):
        created.append(media_type)
        return SimpleNamespace(feed=lambda part: None, finish=lambda: sample, discard=lambda: None)
    monkeypatch.setattr(throughput, "StreamObserver", observer)
    monkeypatch.setattr(c.gateway.throughput, "submit", lambda value, **kwargs:
                        submitted.append(value))
    accounting_complete = threading.Event()
    release = c.gateway._release
    def record_completion(key_id, size):
        release(key_id, size)
        accounting_complete.set()
    monkeypatch.setattr(c.gateway, "_release", record_completion)
    # Native response accounting stays untouched; only rate measurement is
    # excluded. A fake observer would otherwise return a recordable sample.
    response = (b'event: message_start\ndata: {"type":"message_start"}\n\n'
        b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":5000}}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
    c.pool.plans["s1"].append({"chunks": [response]})
    status, data, _ = request(c, path=path)
    assert status == 200 and data == response
    # The client can receive the terminal chunk before the server submits its
    # sample. Wait for final request cleanup for both providers, so absence of
    # Devin accounting is checked only after accounting could have occurred.
    assert accounting_complete.wait(timeout=5), "gateway response accounting did not complete"
    assert len(c.pool.requests) == 1
    assert created == ([] if provider == "devin" else ["text/event-stream"])
    assert submitted == ([] if provider == "devin" else [sample])
    assert c.gateway.throughput._thread is None


@pytest.mark.parametrize("path", ["/v1/messages?beta=false", "/v1/messages?beta=true&extra=1",
    "/v1/messages?", "/v1/messages?url=https://example.test"])
def test_native_arbitrary_query_never_reaches_transport(native_http, path):
    c = native_http
    assert request(c, path=path)[0] == 400
    assert c.pool.attempts == [] and c.pool.requests == []


@pytest.mark.parametrize("plan", [{"pre_send_error": True}, {"ambiguous": True},
    {"status": 503, "retryable": True}])
def test_native_errors_disable_sdk_retry_hint_and_do_not_redispatch(native_http, plan):
    c = native_http
    c.pool.plans["s1"].append(plan)
    status, _, headers = request(c)
    assert status >= 400 and headers["x-should-retry"] == "false"
    assert len(c.pool.attempts) == 1 and len(c.pool.requests) <= 1


def test_binding_change_between_readiness_and_body_cancels_before_delegate(native_http):
    c = native_http
    c.pool.plans["s1"].append({"block_ready": True})
    results = []
    client = threading.Thread(target=lambda: results.append(request(c)), daemon=True)
    client.start()
    wait_for(lambda: bool(c.pool.attempts) and c.pool.attempts[0].ready.is_set())
    bind(c, generation=1, account_fp="e"*64)
    client.join(3)
    assert not client.is_alive() and results[0][0] == 403
    assert c.pool.requests == [] and c.pool.attempts[0].closed.is_set()


def test_snapshot_identity_is_rechecked_even_if_store_active_boolean_is_true(native_http, monkeypatch):
    c = native_http
    target = c.gateway._store_targets(c.key)[0]
    monkeypatch.setattr(c.store, "gateway_request_is_active", lambda *args, **kwargs: True)
    assert c.gateway._authorized(c.key, target)
    current = {**target, "provider_account_fp": "e"*64}
    monkeypatch.setattr(c.store, "gateway_targets", lambda *args, **kwargs: [current])
    assert not c.gateway._authorized(c.key, target)
    assert not c.gateway._authorized({**c.key, "provider": "anthropic"}, target)


@pytest.mark.parametrize("field,value", [("claim_id", "e"*32), ("claim_generation", 44),
    ("provider_binding_generation", 4), ("provider_account_fp", "e"*64),
    ("bridge_contract", "messages-v1"), ("native_version", "3000.11.93"),
    ("adapter_revision", "d"*64), ("model_map_digest", "e"*64)])
def test_native_cache_identities_include_provider_binding_and_runtime(native_http, field, value):
    c = native_http
    target = c.gateway._store_targets(c.key)[0]
    changed = {**target, field: value}
    assert c.gateway._route_identity(c.key, target) != c.gateway._route_identity(c.key, changed)
    assert c.gateway._health_identity(c.key, target) != c.gateway._health_identity(c.key, changed)
    assert c.gateway._cooldown_identity(c.key, target, "model") != c.gateway._cooldown_identity(c.key, changed, "model")


def test_native_provider_session_affinity_and_key_generations_are_isolated(native_http):
    c = native_http
    target = c.gateway._store_targets(c.key)[0]
    other_key = {**c.key, "generation": c.key["generation"]+1}
    assert c.gateway._session_key(c.key, "same") != c.gateway._session_key({**c.key, "provider": "anthropic"}, "same")
    assert c.gateway._route_identity(c.key, target) != c.gateway._route_identity(other_key, target)
    session = c.gateway._session_key(c.key, "same")
    c.gateway._success(session, target, c.key)
    assert c.gateway.affinity[session][:-1] == c.gateway._route_identity(c.key, target)


def native_target(c):
    bind(c)
    key = native_key(c)
    runtime = GatewayRuntime(c.cfg, c.store)
    try:
        return runtime._transport_target(key, native_targets(c, key)[0])
    finally:
        runtime.close()


class PipeProcess:
    def __init__(self, ready):
        raw = json.dumps(ready, separators=(",", ":")).encode()
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(framed(reply(headers={"content-type": "application/json"}))
            + framed(raw) + framed(b""))
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def fake_attempt(controls, monkeypatch, *, ready_changes=None):
    target = native_target(controls)
    attempt = NativeSSHAttempt(target, controls.cfg)
    ready = {**attempt._ready_body(), **(ready_changes or {})}
    process = PipeProcess(ready)
    launched = []
    monkeypatch.setattr(attempt, "_command", lambda: ["FIXED_FAKE_PEER"])
    monkeypatch.setattr(gateway_transport.subprocess, "Popen", lambda *args, **kwargs:
                        launched.append((args, kwargs)) or process)
    return target, attempt, process, launched


def decode_preamble(stream):
    size = struct.unpack("!I", stream.read(4))[0]
    return json.loads(stream.read(size))


def test_native_fixed_command_keeps_host_pin_and_legacy_command_unchanged(controls, tmp_path):
    target = native_target(controls)
    legacy, key = service_attempt(tmp_path)
    native = NativeSSHAttempt({**target, **legacy.target},
                              SimpleNamespace(gateway_ssh_key_path=str(key)))
    try:
        assert legacy._command()[-1] == "ccfleet-gateway-v1"
        command = native._command()
        assert command[-1] == "ccfleet-devin-gateway-v1"
        assert "StrictHostKeyChecking=yes" in command and "ClearAllForwardings=yes" in command
    finally:
        legacy.close()
        native.close()


@pytest.mark.parametrize("path", ["/v1/messages", "/v1/messages?beta=true"])
def test_native_status_and_request_share_detached_binding_only_after_auth(controls, monkeypatch, path):
    target, attempt, process, _ = fake_attempt(controls, monkeypatch)
    native = copy.deepcopy(target["_native_context"])
    target["_native_context"]["binding"]["owner_id"] = "FORGED_AFTER_CONSTRUCTION"
    observed = []
    def authorized():
        observed.append(process.stdin.getvalue())
        return True
    try:
        body = b'{"model":"opus","messages":[{"role":"user","content":"PRIVATE_PROMPT"}]}'
        attempt.start({"content-type": "application/json"}, body, path=path,
                      before_model_send=authorized)
        attempt.writer.join(2)
        assert not attempt.writer.is_alive() and not attempt.write_error
        before = io.BytesIO(observed[0])
        assert decode_preamble(before) == {"version": 2, "operation": "status", "native": native}
        assert before.read() == b""
        stream = io.BytesIO(process.stdin.getvalue())
        first, second = decode_preamble(stream), decode_preamble(stream)
        assert first["native"] == second["native"] == native
        assert second == {"version": 2, "operation": "request", "method": "POST",
            "path": path, "headers": {"content-type": "application/json"},
            "body_size": len(body), "native": native}
        assert stream.read() == body and attempt.model_started
        canonical = json.dumps(native["binding"], ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode()
        assert attempt.binding_digest == hashlib.sha256(canonical).hexdigest()
    finally:
        attempt.close()


@pytest.mark.parametrize("changes", [{"binding_digest": "0"*64}, {"provider": "anthropic"},
    {"bridge_contract": "messages-v1"}, {"ready": 1}, {"protocol": True}, {"extra": True}])
def test_native_forged_ready_never_gets_model_data(controls, monkeypatch, changes):
    _, attempt, process, _ = fake_attempt(controls, monkeypatch, ready_changes=changes)
    try:
        with pytest.raises(GatewayTransportError):
            attempt.start({}, b"PRIVATE_PROMPT")
        assert not attempt.ready and not attempt.model_started
        assert b"PRIVATE_PROMPT" not in process.stdin.getvalue()
        stream = io.BytesIO(process.stdin.getvalue())
        assert decode_preamble(stream)["operation"] == "status" and stream.read() == b""
    finally:
        attempt.close()


def test_native_authorization_recheck_and_count_tokens_prevent_request_frame(controls, monkeypatch):
    _, attempt, process, launched = fake_attempt(controls, monkeypatch)
    try:
        with pytest.raises(GatewayTransportError) as caught:
            attempt.start({}, b"PRIVATE_PROMPT", path="/v1/messages/count_tokens")
        assert caught.value.status == 501 and launched == []
        with pytest.raises(GatewayTransportError) as caught:
            attempt.start({}, b"PRIVATE_PROMPT", before_model_send=lambda: False)
        assert caught.value.status == 403 and attempt.ready and not attempt.model_started
        assert b"PRIVATE_PROMPT" not in process.stdin.getvalue()
    finally:
        attempt.close()


def test_native_partial_request_write_is_never_retryable(controls, monkeypatch):
    _, attempt, process, _ = fake_attempt(controls, monkeypatch)
    class PartialPipe(io.BytesIO):
        writes = 0
        def write(self, data):
            self.writes += 1
            if self.writes > 1:
                super().write(data[:3])
                raise BrokenPipeError("PRIVATE_PIPE_ERROR")
            return super().write(data)
    process.stdin = PartialPipe()
    try:
        attempt.start({}, b"PRIVATE_PROMPT")
        attempt.writer.join(2)
        assert attempt.model_started and attempt.write_error and process.killed
        with pytest.raises(GatewayTransportError) as caught:
            attempt.metadata()
        assert not caught.value.retryable and "PRIVATE_" not in str(caught.value)
    finally:
        attempt.close()


@pytest.mark.parametrize("change", ["request", "binding", "runtime", "target", "claim_bool"])
def test_invalid_native_context_is_rejected_before_any_spawn(controls, monkeypatch, change):
    target = native_target(controls)
    if change == "request":
        target["_native_context"]["request_id"] = "x"*32
    elif change == "binding":
        target["_native_context"]["binding"]["extra"] = "PRIVATE_CONTENT"
    elif change == "runtime":
        target["_native_context"]["runtime"]["bridge_contract"] = "messages-v1"
    elif change == "target":
        target["provider_account_fp"] = "e"*64
    else:
        target["_native_context"]["binding"]["claim_generation"] = True
    monkeypatch.setattr(gateway_transport.subprocess, "Popen", lambda *args, **kwargs:
                        pytest.fail("must not spawn"))
    with pytest.raises(GatewayTransportError) as caught:
        gateway_transport.transport_attempt(target, controls.cfg)
    assert caught.value.status == 503 and "PRIVATE_CONTENT" not in str(caught.value)


def test_default_factory_dispatches_native_and_unknown_provider_fails_closed(controls):
    target = native_target(controls)
    assert isinstance(gateway_transport.transport_attempt(target, controls.cfg), NativeSSHAttempt)
    assert type(gateway_transport.transport_attempt({}, controls.cfg)) is SSHAttempt
    with pytest.raises(GatewayTransportError):
        gateway_transport.transport_attempt({"provider": "private"}, controls.cfg)


def test_native_control_bootstraps_metadata_but_never_invents_an_enrolled_key(controls):
    c = controls
    assert c.store.gateway_native_control("s1") is None
    options = {"allow_devin": True, "provider_runtime": c.runtime, "now": c.now}
    control = c.store.gateway_native_control("s1", **options)
    own = c.store.get_slot("s1")
    assert control == {"claim": {"owner_id": "a1", "slot_id": "s1",
        "claim_id": own["gateway_claim_id"], "claim_generation": own["gateway_generation"]},
        "runtime": c.runtime, "keys": []}
    bind(c)
    key = native_key(c)
    assert c.store.gateway_native_control("s1", **options)["keys"] == [{"key_id": key["id"],
        "key_generation": key["generation"], "provider_generation": 1}]
    c.store.revoke_gateway_key(key["id"], "a1", now=c.now)
    assert c.store.gateway_native_control("s1", **options)["keys"] == []
    c.store.begin_release("s1", held_by="a1")
    assert c.store.gateway_native_control("s1", **options) is None


def native_heartbeat(c, provider):
    token = c.store.rotate_token("m1")
    return call(c.server, "POST", "/api/heartbeat", {"node_id": "m1", "mode": "machine",
        "slots": [{"unix_user": "slot01", "present": True, "gateway_provider": provider}]},
        {"Authorization": "Bearer " + token})


def test_authenticated_machine_provider_report_binds_current_claim_and_revokes_key(native_http):
    c = native_http
    slot = c.store.get_slot("s1")
    envelope = {"claim_id": slot["gateway_claim_id"],
        "claim_generation": slot["gateway_generation"], "report": native_report(c.now)}
    status, raw, _ = native_heartbeat(c, envelope)
    assert status == 200
    desired = json.loads(raw)["desired"]["slots"][0]
    assert desired["state"] == "claimed" and desired["devin_gateway"]["keys"]
    assert "account_fp" not in raw.decode() and "credential_sha256" not in raw.decode()
    assert c.store.gateway_provider_binding("s1", "a1", "devin")["identity_verified"] is True
    envelope["report"] = native_report(c.now, account_fp=None, identity_verified=False,
                                        capabilities={})
    status, raw, _ = native_heartbeat(c, envelope)
    assert status == 200 and json.loads(raw)["desired"]["slots"][0]["devin_gateway"]["keys"] == []
    current = c.store.resolve_gateway_key(c.key["key_token"], now=c.now)
    assert current["generation"] > c.key["generation"]
    assert c.gateway._store_targets(current) == []


@pytest.mark.parametrize("case", ["claim", "generation", "runtime", "extra", "old"])
def test_stale_foreign_or_malformed_machine_evidence_cannot_change_binding(native_http, case):
    c = native_http
    slot = c.store.get_slot("s1")
    value = {"claim_id": slot["gateway_claim_id"], "claim_generation": slot["gateway_generation"],
             "report": native_report(c.now, account_fp="e"*64)}
    if case == "claim":
        value["claim_id"] = "f"*32
    elif case == "generation":
        value["claim_generation"] = True
    elif case == "runtime":
        value["report"]["adapter_revision"] = "e"*64
    elif case == "extra":
        value["credential_sha256"] = "PRIVATE_CREDENTIAL_GUARD"
    else:
        value["report"]["observed_at"] = c.now-301
    assert native_heartbeat(c, value)[0] == 200
    binding = c.store.gateway_provider_binding("s1", "a1", "devin")
    assert binding["account_fp"] == "a"*64 and binding["generation"] == 1
    assert "PRIVATE_CREDENTIAL_GUARD" not in repr(c.store.recent_heartbeats("m1"))
