"""Live HTTP requests through the production dispatcher, storage and slot boundary.

The slot transport is injected: these checks exercise HTTP/routing/isolation,
while test_gateway_security separately checks the real subprocess wire protocol.
"""

import base64
import collections
import contextlib
import hashlib
import http.client
import json
import socket
import struct
import sys
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ccfleetd import slots
from ccfleetd.api import Context, build_server
from ccfleetd.gateway import GatewayRuntime
from ccfleetd.gateway_transport import GatewayTransportError, SSHAttempt
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.store import Store

STREAM = b'event: message_start\ndata: {"type":"message_start"}\n\nevent: message_stop\ndata: {"type":"message_stop"}\n\n'


def public_key(byte=1):
    kind = b"ssh-ed25519"
    blob = struct.pack("!I", len(kind)) + kind + struct.pack("!I", 32) + bytes([byte]) * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode()


def held_slots(store):
    store.add_account("owner-a", "sub-a", "owner-a@example.com", slot_quota=2, now=1)
    store.add_account("owner-b", "sub-b", "owner-b@example.com", slot_quota=1, now=1)
    for index in range(1, 4):
        node = f"gate-{index}"
        store.add_node(node, "operator", now=1)
        store.set_node_access(node, f"192.0.2.{index}", 2222, public_key(index + 10))
        store.add_slot(f"s{index}", node, "slot01", now=1)
        store.apply_slot_report(node, [{"unix_user": "slot01", "present": False}], now=2)
    for owner, stamp in (("owner-a", 3), ("owner-a", 4), ("owner-b", 5)):
        slot = store.claim_slot(owner, now=stamp)
        store.move_slot(slot["id"], slots.CLAIMED)
        store.move_slot(slot["id"], slots.ACTIVE)
    own = [s["id"] for s in store.list_slots(held_by="owner-a")]
    foreign = store.list_slots(held_by="owner-b")[0]["id"]
    return own, foreign


class Clock:
    def __init__(self):
        self.offset = 0

    def __call__(self):
        return time.monotonic() + self.offset


class Attempt:
    def __init__(self, pool, target, plan):
        self.pool, self.target, self.plan = pool, target, plan
        self.closed = threading.Event()
        self.started = threading.Event()
        self.ready = threading.Event()

    def start(self, headers, body, *, path, before_model_send=lambda: True):
        if self.plan.get("pre_send_error"):
            raise GatewayTransportError("PRIVATE_NODE_AND_CREDENTIAL", retryable=True)
        self.ready.set()
        if self.plan.get("block_ready"):
            self.closed.wait(5)
        if not before_model_send() or self.closed.is_set():
            raise GatewayTransportError("PRIVATE_CANCELLED_REQUEST", status=403)
        self.pool.requests.append({"slot": self.target["slot_id"], "path": path,
                                   "headers": dict(headers), "body": json.loads(body)})
        self.started.set()
        if self.plan.get("ambiguous"):
            raise GatewayTransportError("PRIVATE_POST_FAILURE", retryable=False)

    def metadata(self):
        return {"version": 2, "status": self.plan.get("status", 200),
                "headers": {"content-type": self.plan.get("content_type", "text/event-stream")},
                "retryable": self.plan.get("retryable", False)}

    def chunks(self):
        if self.plan.get("block"):
            self.closed.wait(5)
            raise GatewayTransportError("PRIVATE_BLOCKED_TRANSPORT")
        yield from self.plan.get("chunks", [STREAM])
        if self.plan.get("truncated"):
            raise GatewayTransportError("PRIVATE_TRUNCATED_STREAM")

    def finish(self, _status=200):
        if self.plan.get("finish_error"):
            raise GatewayTransportError("PRIVATE_FINISH_ERROR")

    def close(self):
        self.closed.set()


class Pool:
    def __init__(self):
        self.plans = collections.defaultdict(collections.deque)
        self.requests, self.attempts = [], []

    def __call__(self, target, _cfg):
        planned = self.plans[target["slot_id"]]
        attempt = Attempt(self, target, planned.popleft() if planned else {})
        self.attempts.append(attempt)
        return attempt


@pytest.fixture
def scenario(cfg, tmp_path, request):
    private = tmp_path / "service-key"
    private.write_text("SYNTHETIC_UNUSED_SERVICE_KEY")
    private.chmod(0o600)
    configured = replace(cfg, public_url="https://fleet.example.com", gateway_enabled=True,
                         gateway_ssh_key_path=str(private), gateway_ssh_public_key=public_key(9))
    if hasattr(request, "param"):
        configured = replace(configured, **request.param)
    store = Store(":memory:", max_slots_per_machine=8)
    own, foreign = held_slots(store)
    key = store.create_gateway_key("owner-a", "This computer", own, mode="sticky", now=6)
    pool, clock = Pool(), Clock()
    runtime = GatewayRuntime(configured, store, transport_factory=pool, clock=clock)
    ctx = Context(store, configured, Monitor(store, configured, LogNotifier()))
    ctx.gateway = runtime
    server = build_server(ctx, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(cfg=configured, store=store, own=own, foreign=foreign, key=key,
                          pool=pool, runtime=runtime, clock=clock, server=server, ctx=ctx)
    for attempt in pool.attempts:
        attempt.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    runtime.close()
    store.close()


def request(scenario, body=None, *, headers=None, token=None, path="/v1/messages", auth_kind="bearer"):
    if body is None:
        body = {"model": "opus", "max_tokens": 32,
                "messages": [{"role": "user", "content": "question"}], "stream": True}
    raw = json.dumps(body).encode() if isinstance(body, dict) else body
    auth = scenario.key["key_token"] if token is None else token
    credential = {"Authorization": "Bearer " + auth} if auth_kind == "bearer" else {"x-api-key": auth}
    supplied = {**credential, "Content-Type": "application/json",
                "anthropic-version": "2023-06-01", **(headers or {})}
    conn = http.client.HTTPConnection(*scenario.server.server_address, timeout=3)
    conn.request("POST", path, body=raw, headers=supplied)
    answer = conn.getresponse()
    data = answer.read()
    result = answer.status, data, dict(answer.getheaders())
    conn.close()
    return result


def raw_request(scenario, fields, *, target="/v1/messages", body=b'{}', token=None):
    sock = socket.create_connection(scenario.server.server_address, timeout=3)
    sock.sendall(("POST " + target + " HTTP/1.1\r\nHost: fleet.example.com\r\n"
                  "Authorization: Bearer " + (token or scenario.key["key_token"]) + "\r\n"
                  + fields + "\r\n").encode() + body)
    reply = bytearray()
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            reply.extend(chunk)
    except ConnectionResetError:
        pass
    finally:
        sock.close()
    return bytes(reply)


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "expected asynchronous gateway event did not occur"


def test_native_messages_traverse_live_http_with_context_preserved_and_identity_removed(scenario):
    context = {"model": "opus", "max_tokens": 32,
               "system": "Directory /Users/private; user_id is application text",
               "messages": [{"role": "user", "content": "question"}],
               "tools": [{"name": "file", "input_schema": {"properties": {"device_id": {}}}}],
               "stream": True}
    status, body, headers = request(scenario, {**context, "metadata": {
        "user_id": "PRIVATE_USER", "session_id": "conversation-1"}, "device_id": "PRIVATE_DEVICE"},
        headers={"user-agent": "PRIVATE_LAPTOP", "Cookie": "PRIVATE_COOKIE",
                 "X-Forwarded-For": "PRIVATE_IP", "X-CCFleet-Session": "conversation-1"})
    assert status == 200 and body == STREAM
    assert headers["Transfer-Encoding"] == "chunked" and headers["Cache-Control"] == "no-store"
    assert scenario.pool.requests == [{"slot": scenario.own[0], "path": "/v1/messages",
                                      "headers": {"content-type": "application/json",
                                                  "anthropic-version": "2023-06-01"},
                                      "body": context}]
    assert scenario.key["key_token"] not in json.dumps(scenario.pool.requests)
    wait_for(lambda: scenario.runtime.active == scenario.runtime.body_bytes == 0)


@pytest.mark.parametrize("auth_kind", ["bearer", "api-key"])
def test_native_bearer_and_sdk_api_key_authentication_are_both_supported(scenario, auth_kind):
    assert request(scenario, auth_kind=auth_kind)[0:2] == (200, STREAM)
    assert scenario.key["key_token"] not in json.dumps(scenario.pool.requests)


@pytest.mark.parametrize("status", [401, 403, 429, 502, 503, 504, 529])
def test_explicit_retryable_error_uses_only_the_next_selected_account(scenario, status):
    first, backup = scenario.own
    scenario.pool.plans[first].append({"status": status, "retryable": True,
                                       "chunks": [b"PRIVATE_PROVIDER_ERROR"]})
    assert request(scenario)[0:2] == (200, STREAM)
    assert [r["slot"] for r in scenario.pool.requests] == [first, backup]
    assert scenario.foreign not in [r["slot"] for r in scenario.pool.requests]
    wait_for(lambda: all(a.closed.is_set() for a in scenario.pool.attempts))


def test_broken_primary_before_model_send_fails_over_without_replaying_a_post(scenario):
    first, backup = scenario.own
    scenario.pool.plans[first].append({"pre_send_error": True})
    assert request(scenario)[0:2] == (200, STREAM)
    assert [a.target["slot_id"] for a in scenario.pool.attempts] == [first, backup]
    assert [r["slot"] for r in scenario.pool.requests] == [backup]


def test_ambiguous_failure_after_model_send_does_not_try_backup_or_expose_private_errors(scenario):
    first, _ = scenario.own
    scenario.pool.plans[first].append({"ambiguous": True})
    status, body, _ = request(scenario)
    assert status == 502 and json.loads(body)["type"] == "error"
    assert b"PRIVATE_" not in body
    assert [r["slot"] for r in scenario.pool.requests] == [first]


def test_pin_uses_only_one_selected_slot_even_when_it_returns_retryable_error(scenario):
    first, backup = scenario.own
    scenario.pool.plans[backup].append({"status": 429, "retryable": True,
                                        "content_type": "application/json",
                                        "chunks": [b'{"type":"error","error":{"type":"rate_limit_error"}}']})
    assert request(scenario, headers={"X-CCFleet-Slot": backup})[0] == 429
    assert [r["slot"] for r in scenario.pool.requests] == [backup]
    assert first not in [r["slot"] for r in scenario.pool.requests]


@pytest.mark.parametrize("slot", ["foreign", "excluded"])
def test_pin_cannot_expand_the_key_pool(scenario, slot):
    pin = scenario.foreign if slot == "foreign" else scenario.own[1]
    if slot == "excluded":
        scenario.store.update_gateway_key(scenario.key["id"], "owner-a", scenario.own[:1], "priority")
    status, body, _ = request(scenario, headers={"X-CCFleet-Slot": pin})
    assert status == 403 and json.loads(body)["type"] == "error"
    assert not scenario.pool.attempts


def test_single_selected_slot_never_broadens_to_another_owned_slot(scenario):
    first, _ = scenario.own
    scenario.store.update_gateway_key(scenario.key["id"], "owner-a", [first], "priority")
    scenario.pool.plans[first].append({"status": 429, "retryable": True,
                                       "content_type": "application/json",
                                       "chunks": [b'{"type":"error","error":{"type":"rate_limit_error"}}']})
    assert request(scenario)[0] == 429
    assert [r["slot"] for r in scenario.pool.requests] == [first]


def test_sticky_conversation_stays_on_backup_after_primary_recovers(scenario):
    first, backup = scenario.own
    scenario.pool.plans[first].append({"status": 429, "retryable": True})
    assert request(scenario, headers={"X-CCFleet-Session": "same-conversation"})[0] == 200
    scenario.clock.offset = 60
    assert request(scenario, headers={"X-CCFleet-Session": "same-conversation"})[0] == 200
    assert request(scenario, headers={"X-CCFleet-Session": "different-conversation"})[0] == 200
    assert [r["slot"] for r in scenario.pool.requests] == [first, backup, backup, first]
    assert "same-conversation" not in repr(scenario.runtime.affinity)


def test_identical_session_id_under_another_key_does_not_inherit_affinity(scenario):
    first, backup = scenario.own
    other = scenario.store.create_gateway_key("owner-a", "Other device", scenario.own,
                                              mode="sticky", now=7)
    scenario.pool.plans[first].append({"status": 429, "retryable": True})
    assert request(scenario, headers={"X-CCFleet-Session": "same-id"})[0] == 200
    assert request(scenario, headers={"X-CCFleet-Session": "same-id"}, token=other["key_token"])[0] == 200
    assert [r["slot"] for r in scenario.pool.requests] == [first, backup, first]


def test_priority_mode_returns_to_primary_when_it_is_eligible(scenario):
    first, backup = scenario.own
    scenario.store.update_gateway_key(scenario.key["id"], "owner-a", scenario.own, "priority")
    scenario.pool.plans[first].append({"status": 429, "retryable": True})
    assert request(scenario, headers={"X-CCFleet-Session": "same-id"})[0] == 200
    scenario.clock.offset = 60
    assert request(scenario, headers={"X-CCFleet-Session": "same-id"})[0] == 200
    assert [r["slot"] for r in scenario.pool.requests] == [first, backup, first]


@pytest.mark.parametrize("path", ["/v1/messages/count_tokens", "/v1/messages?beta=true",
                                  "/v1/messages/count_tokens?beta=true"])
def test_documented_native_message_and_token_paths_reach_the_selected_slot(scenario, path):
    assert request(scenario, path=path)[0] == 200
    assert scenario.pool.requests[0]["path"] == path


@pytest.mark.parametrize("stage", ["truncated", "finish_error", "headers-only"])
def test_committed_stream_failure_has_no_success_terminator_or_backup(scenario, stage):
    first, _ = scenario.own
    scenario.pool.plans[first].append({"truncated": True, "chunks": []} if stage == "headers-only" else {stage: True})
    with pytest.raises(http.client.IncompleteRead):
        request(scenario)
    assert [r["slot"] for r in scenario.pool.requests] == [first]
    wait_for(lambda: all(a.closed.is_set() for a in scenario.pool.attempts))


@pytest.mark.parametrize("change", ["revoke", "selection", "disable", "release"])
def test_permission_change_cancels_an_already_open_stream_without_failover(scenario, change):
    first, _ = scenario.own
    scenario.pool.plans[first].append({"block": True})
    conn = http.client.HTTPConnection(*scenario.server.server_address, timeout=3)
    conn.request("POST", "/v1/messages", body=b'{"model":"opus","messages":[]}',
                 headers={"Authorization": "Bearer " + scenario.key["key_token"],
                          "Content-Type": "application/json"})
    answer = conn.getresponse()
    assert answer.status == 200
    wait_for(lambda: scenario.pool.attempts and scenario.pool.attempts[0].started.is_set())
    if change == "revoke":
        scenario.store.revoke_gateway_key(scenario.key["id"], "owner-a")
    elif change == "selection":
        scenario.store.update_gateway_key(scenario.key["id"], "owner-a", scenario.own[1:], "priority")
    elif change == "disable":
        scenario.store.set_enabled(scenario.store.get_slot(first)["node_id"], False)
    else:
        scenario.store.begin_release(first, held_by="owner-a")
    wait_for(lambda: scenario.pool.attempts[0].closed.is_set())
    with pytest.raises(http.client.IncompleteRead):
        answer.read()
    conn.close()
    assert [r["slot"] for r in scenario.pool.requests] == [first]
    wait_for(lambda: scenario.runtime.active == scenario.runtime.body_bytes == 0)


def test_client_disconnect_cancels_blocked_transport_and_releases_capacity(scenario):
    first, _ = scenario.own
    scenario.pool.plans[first].append({"block": True})
    sock = socket.create_connection(scenario.server.server_address, timeout=3)
    body = b'{"model":"opus","messages":[]}'
    sock.sendall(("POST /v1/messages HTTP/1.1\r\nHost: fleet.example.com\r\n"
                  "Authorization: Bearer " + scenario.key["key_token"] + "\r\n"
                  "Content-Type: application/json\r\nContent-Length: " + str(len(body))
                  + "\r\n\r\n").encode() + body)
    wait_for(lambda: scenario.pool.attempts and scenario.pool.attempts[0].started.is_set())
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    sock.close()
    wait_for(lambda: scenario.pool.attempts[0].closed.is_set())
    wait_for(lambda: scenario.runtime.active == scenario.runtime.body_bytes == 0)
    assert len(scenario.pool.requests) == 1


@pytest.mark.parametrize("kind", ["legacy", "json"])
def test_native_claude_session_metadata_drives_affinity_without_leaving_identity_upstream(scenario, kind):
    first, backup = scenario.own
    scenario.pool.plans[first].append({"status": 429, "retryable": True})
    session = "12345678-1234-1234-1234-123456789abc"
    user = ("user_PRIVATE_DEVICE_account_PRIVATE_ACCOUNT_session_" + session if kind == "legacy"
            else json.dumps({"device_id": "PRIVATE_DEVICE", "account_uuid": "PRIVATE_ACCOUNT",
                             "session_id": session}))
    body = {"model": "opus", "messages": [], "metadata": {"user_id": user}}
    assert request(scenario, body)[0] == 200
    scenario.clock.offset = 60
    assert request(scenario, body)[0] == 200
    assert [r["slot"] for r in scenario.pool.requests] == [first, backup, backup]
    assert all("metadata" not in r["body"] for r in scenario.pool.requests)
    assert session not in repr(scenario.runtime.affinity)


def blocked_request(scenario, key=None, body=None):
    first = scenario.own[0]
    scenario.pool.plans[first].append({"block": True})
    conn = http.client.HTTPConnection(*scenario.server.server_address, timeout=3)
    encoded = body or b'{"model":"opus","messages":[]}'
    conn.request("POST", "/v1/messages", body=encoded,
                 headers={"Authorization": "Bearer " + (key or scenario.key["key_token"]),
                          "Content-Type": "application/json"})
    answer = conn.getresponse()
    assert answer.status == 200
    wait_for(lambda: scenario.runtime.active == 1)
    return conn, answer


def test_per_key_capacity_is_bounded_without_blocking_another_key(scenario):
    scenario.runtime.cfg = replace(scenario.cfg, gateway_max_active=2, gateway_max_per_key=1)
    other = scenario.store.create_gateway_key("owner-a", "Second key", scenario.own,
                                              mode="priority", now=7)
    conn, answer = blocked_request(scenario)
    try:
        assert request(scenario)[0] == 503
        assert len(scenario.pool.requests) == 1
        assert request(scenario, token=other["key_token"])[0] == 200
    finally:
        answer.close()
        conn.close()
    wait_for(lambda: scenario.runtime.active == 0)
    assert request(scenario)[0] == 200


def test_global_capacity_is_bounded_across_distinct_keys(scenario):
    scenario.runtime.cfg = replace(scenario.cfg, gateway_max_active=1, gateway_max_per_key=1)
    other = scenario.store.create_gateway_key("owner-a", "Second key", scenario.own,
                                              mode="priority", now=7)
    conn, answer = blocked_request(scenario)
    try:
        assert request(scenario, token=other["key_token"])[0] == 503
        assert len(scenario.pool.requests) == 1
    finally:
        answer.close()
        conn.close()
    wait_for(lambda: scenario.runtime.active == 0)


def test_total_admitted_body_budget_rejects_before_an_extra_model_request(scenario):
    scenario.runtime.cfg = replace(scenario.cfg, gateway_body_budget_bytes=1100)
    body = json.dumps({"model": "opus", "messages": [{"role": "user", "content": "x" * 900}]}).encode()
    conn, answer = blocked_request(scenario, body=body)
    try:
        assert request(scenario, body)[0] == 503
        assert len(scenario.pool.requests) == 1
    finally:
        answer.close()
        conn.close()
    wait_for(lambda: scenario.runtime.active == scenario.runtime.body_bytes == 0)


def test_revocation_during_readiness_cancels_before_any_model_data(scenario):
    first, _ = scenario.own
    scenario.pool.plans[first].append({"block_ready": True})
    done, outcome = threading.Event(), []

    def send():
        try:
            outcome.append(request(scenario))
        finally:
            done.set()

    worker = threading.Thread(target=send, daemon=True)
    worker.start()
    wait_for(lambda: scenario.pool.attempts and scenario.pool.attempts[0].ready.is_set())
    scenario.store.revoke_gateway_key(scenario.key["id"], "owner-a")
    assert done.wait(3)
    worker.join(timeout=1)
    assert outcome[0][0] == 403
    assert not scenario.pool.requests
    assert len(scenario.pool.attempts) == 1
    wait_for(lambda: scenario.runtime.active == scenario.runtime.body_bytes == 0)


def test_disconnect_during_readiness_prevents_a_model_post_or_backup(scenario):
    first, _ = scenario.own
    scenario.pool.plans[first].append({"block_ready": True})
    body = b'{"model":"opus","messages":[]}'
    sock = socket.create_connection(scenario.server.server_address, timeout=3)
    sock.sendall(("POST /v1/messages HTTP/1.1\r\nHost: fleet.example.com\r\n"
                  "Authorization: Bearer " + scenario.key["key_token"] + "\r\n"
                  "Content-Type: application/json\r\nContent-Length: " + str(len(body))
                  + "\r\n\r\n").encode() + body)
    wait_for(lambda: scenario.pool.attempts and scenario.pool.attempts[0].ready.is_set())
    sock.shutdown(socket.SHUT_RDWR)
    sock.close()
    wait_for(lambda: scenario.pool.attempts[0].closed.is_set())
    wait_for(lambda: scenario.runtime.active == scenario.runtime.body_bytes == 0)
    assert not scenario.pool.requests
    assert len(scenario.pool.attempts) == 1


SLOT_HARNESS = r'''
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
from ccfleet_agent import local_relay as relay
home, capture = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
class Upstream:
    sock = None
    status = 200
    length = None
    def __init__(self):
        self.parts = iter([b'event: message_start\ndata: {"type":"message_start"}\n\n',
                           b'event: message_stop\ndata: {"type":"message_stop"}\n\n'])
    def connect(self):
        pass
    def request(self, method, path, *, body, headers):
        capture.write_text(json.dumps({"method":method,"path":path,"body":json.loads(body),
                                       "headers":headers}))
    def getresponse(self):
        return self
    def getheaders(self):
        return [("content-type", "text/event-stream"), ("request-id", "PRIVATE_UPSTREAM_ID")]
    def read1(self, size):
        return next(self.parts, b"")
    def close(self):
        pass
code = relay.serve_one(sys.stdin.buffer, sys.stdout.buffer, home, gateway=True,
                       phase="ready", policy=lambda: None)
if code == 0:
    code = relay.serve_one(sys.stdin.buffer, sys.stdout.buffer, home, gateway=True,
                           phase="request", policy=lambda: None, connect=Upstream)
sys.exit(code)
'''


def test_live_http_through_real_subprocess_protocol_and_slot_relay_uses_only_slot_oauth(scenario, tmp_path):
    """The network upstream is stubbed; both production transport/relay run."""
    from pathlib import Path

    home = tmp_path.resolve() / "slot-native-home"
    (home / ".claude").mkdir(parents=True)
    (home / ".config/ccfleet").mkdir(parents=True)
    account = "SYNTHETIC_BOUND_SLOT_ACCOUNT"
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": account}}))
    (home / ".config/ccfleet/slot-state.json").write_text(json.dumps({
        "bound_fp": hashlib.sha256(account.encode()).hexdigest()[:16]}))
    slot_token = "SYNTHETIC_SLOT_OAUTH_TOKEN"
    (home / ".claude/.credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": slot_token, "refreshToken": "SYNTHETIC_REFRESH_NEVER_FORWARDED",
        "expiresAt": (time.time() + 3600) * 1000}}))
    script = tmp_path / "slot-harness.py"
    script.write_text(SLOT_HARNESS)
    capture = tmp_path / "actual-upstream-request.json"
    root = Path(__file__).resolve().parents[1]

    def transport(target, cfg):
        attempt = SSHAttempt(target, cfg)
        attempt._command = lambda: [sys.executable, "-u", str(script), str(root), str(home), str(capture)]
        return attempt

    scenario.runtime.transport_factory = transport
    context = {"model": "opus", "messages": [{"role": "user", "content": "question"}], "stream": True}
    status, body, response_headers = request(scenario, {**context, "metadata": {"user_id": "PRIVATE_LOCAL_ID"}},
                                            headers={"User-Agent": "PRIVATE_LOCAL_CLIENT"})
    assert status == 200 and body == STREAM
    forwarded = json.loads(capture.read_text())
    assert forwarded["method"] == "POST" and forwarded["path"] == "/v1/messages"
    assert forwarded["body"] == context
    assert forwarded["headers"]["authorization"] == "Bearer " + slot_token
    assert forwarded["headers"]["user-agent"] == "ccfleet-slot-relay/2"
    assert "PRIVATE_" not in json.dumps(forwarded)
    assert scenario.key["key_token"] not in json.dumps(forwarded)
    assert "SYNTHETIC_REFRESH_NEVER_FORWARDED" not in json.dumps(forwarded)
    assert "request-id" not in {name.lower() for name in response_headers}
    assert slot_token.encode() not in body
