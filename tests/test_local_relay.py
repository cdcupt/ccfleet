"""Inference v2 filtering, slot isolation, streaming and legacy-route retirement."""

import hashlib
import http.client
import io
import json
import math
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleet_agent import local_relay

relay = local_relay


@pytest.fixture
def slot(tmp_path):
    home = tmp_path.resolve() / "slot"
    (home / ".claude").mkdir(parents=True)
    (home / ".config/ccfleet").mkdir(parents=True)
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "owner-a"}}))
    fingerprint = hashlib.sha256(b"owner-a").hexdigest()[:16]
    (home / ".config/ccfleet/slot-state.json").write_text(json.dumps({"bound_fp": fingerprint}))
    write_credentials(home)
    return home


def write_credentials(home, token="slot-test-token", expiry=None):
    (home / ".claude/.credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": token, "refreshToken": "never-forward-this-refresh-token",
        "expiresAt": expiry if expiry is not None else (time.time() + 3600) * 1000}}))


def request(body=b'{"model":"opus","messages":[]}', **changes):
    meta = {"version": 2, "operation": "request", "method": "POST", "path": "/v1/messages",
            "headers": {"content-type": "application/json", "anthropic-version": "2023-06-01",
                        "anthropic-beta": "real-protocol-beta", "accept": "text/event-stream"},
            "body_size": len(body), **changes}
    return frame(meta) + body


def frame(value):
    raw = json.dumps(value).encode()
    return struct.pack("!I", len(raw)) + raw


def response(raw):
    stream = io.BytesIO(raw)
    header = relay.read_metadata(stream)
    chunks = []
    while True:
        length = struct.unpack("!I", relay.read_exact(stream, 4))[0]
        assert length <= relay.CHUNK_SIZE
        if not length:
            break
        chunks.append(relay.read_exact(stream, length))
    assert stream.read() == b""
    return header, b"".join(chunks)


class UpstreamSocket:
    def __init__(self):
        self.cancelled = threading.Event()
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value

    def shutdown(self, how):
        assert how == socket.SHUT_RDWR
        self.cancelled.set()


class Upstream:
    def __init__(self, status=200, chunks=None, headers=None, failure=None):
        self.status = status
        self.chunks = iter(chunks or [b"event: message_start\n\n", b"event: message_stop\n\n"])
        self.headers = headers or [("Content-Type", "text/event-stream"), ("request-id", "safe-id")]
        self.failure = failure
        self.calls = []
        self.closed = False
        self.length = None
        self.sock = UpstreamSocket()
        self.read_sizes = []
        self.read_bytes = 0
        self.pending = b""

    def connect(self):
        if self.failure == "connect":
            raise OSError("private upstream address and credential detail")

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    def getresponse(self):
        if self.failure == "response":
            raise http.client.HTTPException("private raw response")
        return self

    def getheaders(self):
        return self.headers

    def read1(self, size):
        assert 0 < size <= relay.CHUNK_SIZE
        self.read_sizes.append(size)
        try:
            data = self.pending or next(self.chunks)
            self.pending = data[size:]
            self.read_bytes += len(data[:size])
            return data[:size]
        except StopIteration:
            if self.failure == "truncated":
                raise http.client.IncompleteRead(b"private partial data") from None
            return b""

    def close(self):
        self.closed = True


def run(slot, data=None, upstream=None, **kwargs):
    output = io.BytesIO()
    upstream = upstream or Upstream()
    code = relay.serve_one(io.BytesIO(request() if data is None else data), output, slot,
                           policy=lambda: None, connect=lambda: upstream, **kwargs)
    return code, output.getvalue(), upstream


def test_retired_relay_does_not_read_credentials_or_open_network(capsys):
    assert local_relay.main() == 2
    assert "retired" in capsys.readouterr().err


def test_old_forced_command_has_no_inference_route():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-I", str(root / "ccfleet_agent/local_relay.py")],
        input=b'not-a-model-request', capture_output=True, timeout=5)
    assert result.returncode == 2 and result.stdout == b""
    assert b"retired" in result.stderr


def test_headers_and_structured_identity_are_removed_but_model_context_is_preserved(slot):
    context = {"model": "opus", "system": "Working directory /Users/example; Darwin",
               "messages": [{"role": "user", "content": "my user_id is application data"}],
               "tools": [{"name": "tool", "input_schema": {"properties": {"user_id": {}}}}]}
    body = {**context, "metadata": {"user_id": "laptop-id", "session_id": "local-session",
                                    "unknown_future_fingerprint": "private"},
            **{name: "LOCAL-STRUCTURED-IDENTITY" for name in relay.IDENTITY_FIELDS}}
    headers = {"content-type": "application/json", "accept": "text/event-stream",
               "anthropic-version": "2023-06-01", "anthropic-beta": "semantic-beta",
               "user-agent": "LOCAL-CLI-VERSION", "x-stainless-os": "LOCAL-OS",
               "x-stainless-arch": "LOCAL-ARCH", "x-app": "local-value",
               "x-forwarded-for": "PRIVATE-IP", "forwarded": "PRIVATE-LOCATION",
               "authorization": "Bearer LOCAL-AUTH", "x-api-key": "LOCAL-API-KEY",
               "cookie": "LOCAL-COOKIE", "host": "caller-selected.invalid",
               "x-client-request-id": "LOCAL-SESSION",
               "x-future-unknown-fingerprint": "LOCAL-FUTURE-IDENTITY"}
    code, raw, upstream = run(slot, request(json.dumps(body).encode(), headers=headers))
    assert code == 0 and response(raw)[0]["status"] == 200
    args, forwarded = upstream.calls[0]
    assert args == ("POST", "/v1/messages")
    assert json.loads(forwarded["body"]) == context
    assert forwarded["headers"] == {
        "content-type": "application/json", "accept": "text/event-stream",
        "anthropic-version": "2023-06-01", "anthropic-beta": "semantic-beta",
        "accept-encoding": "identity", "user-agent": "ccfleet-slot-relay/2",
        "x-app": "cli", "authorization": "Bearer slot-test-token"}
    assert "LOCAL-" not in json.dumps(forwarded["headers"])
    assert b"never-forward-this-refresh-token" not in raw
    assert upstream.closed and upstream.sock.timeout == relay.READ_TIMEOUT


def test_status_has_no_model_call_account_label_or_credential(slot):
    code, raw, upstream = run(slot, frame({"version": 2, "operation": "status"}))
    meta, body = response(raw)
    assert code == 0 and upstream.calls == []
    assert meta == {"version": 2, "status": 200, "headers": {"content-type": "application/json"}}
    assert json.loads(body) == {"ready": True, "protocol": 2}
    assert b"owner-a" not in raw and b"slot-test-token" not in raw


def test_disabled_policy_runs_before_input_or_credentials(tmp_path):
    source, output = io.BytesIO(b"must not read"), io.BytesIO()

    def disabled():
        raise relay.RelayError(403, "local inference is not enabled for this slot")

    assert relay.serve_one(source, output, tmp_path, policy=disabled) == 2
    assert source.tell() == 0 and response(output.getvalue())[0]["status"] == 403


def test_missing_operator_policy_fails_closed(tmp_path):
    with pytest.raises(relay.RelayError) as error:
        relay.require_enabled(tmp_path / "missing")
    assert error.value.status == 403


@pytest.mark.parametrize("allowed", [True, False])
def test_gate_delegates_to_shared_policy_and_reports_safe_denial(monkeypatch, allowed):
    directory = Path("/gate")
    calls = []
    monkeypatch.setattr(relay.pwd, "getpwuid", lambda _: SimpleNamespace(pw_name="slot01"))

    def policy(path, user):
        calls.append((path, user))
        if not allowed:
            raise relay.inference_policy.PolicyError("private operator detail")

    monkeypatch.setattr(relay.inference_policy, "require_enabled", policy)
    if allowed:
        relay.require_enabled(directory)
    else:
        with pytest.raises(relay.RelayError) as error:
            relay.require_enabled(directory)
        assert error.value.status == 403 and "private" not in str(error.value)
    assert calls == [(directory, "slot01")]


@pytest.mark.parametrize("changes", [
    {"version": 1}, {"version": True}, {"operation": "status", "extra": "private"},
    {"method": "GET"}, {"path": "https://example.invalid/v1/messages"},
    {"path": "/v1/messages?destination=other"}, {"path": "/v1/messages/../admin"},
    {"body_size": True}, {"body_size": 0}, {"body_size": relay.MAX_BODY + 1},
    {"headers": {"content-type": "text/plain"}},
    {"headers": {"content-type": "application/json", "User-Agent": "a", "user-agent": "b"}},
    {"headers": {"content-type": "application/json", "x-stainless-os": "bad\nheader"}},
    {"host": "example.invalid"},
])
def test_invalid_requests_never_reach_the_upstream(slot, changes):
    code, raw, upstream = run(slot, request(**changes))
    assert code == 2 and response(raw)[0]["status"] in (400, 413)
    assert upstream.calls == []


@pytest.mark.parametrize("raw", [b"{}", b'{"version":2,"version":2}', b'[]',
                                  b'{"version":NaN}', b'\xff'])
def test_strict_frame_json_rejects_ambiguity(slot, raw):
    code, output, upstream = run(slot, struct.pack("!I", len(raw)) + raw)
    assert code == 2 and response(output)[0]["status"] == 400
    assert upstream.calls == []


@pytest.mark.parametrize("body", [b"[]", b'{"model":"a","model":"b"}',
                                   b'{"messages":[{"role":"x","role":"y"}]}',
                                   b'{"metadata":"private-id"}', b'{"max_tokens":NaN}',
                                   b'{"temperature":1e999}', b'{"model":"\\ud800"}', b"{broken"])
def test_unambiguous_body_required_before_model_post(slot, body):
    code, raw, upstream = run(slot, request(body))
    assert code == 2 and response(raw)[0]["status"] == 400
    assert upstream.calls == []


def test_request_above_previous_twenty_megabyte_cap_is_supported(slot):
    body = json.dumps({"messages": [{"role": "user", "content": "x" * (21 * 1024 * 1024)}]}).encode()
    code, _, upstream = run(slot, request(body))
    assert code == 0 and len(upstream.calls[0][1]["body"]) > 20 * 1024 * 1024


@pytest.mark.parametrize("expiry", [True, "soon", -1, 0, float("nan"), float("inf"), 10**1000])
def test_invalid_native_expiry_fails_closed_without_refresh(slot, expiry):
    write_credentials(slot, expiry=expiry)
    source = slot / ".claude/.credentials.json"
    before = source.read_bytes(), source.stat().st_mtime_ns
    code, raw, upstream = run(slot)
    assert code == 2 and response(raw)[0]["status"] == 401
    assert upstream.calls == []
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before


@pytest.mark.parametrize("token", ["", "bad\nvalue", "unicode-中文", "x" * 8193])
def test_invalid_slot_token_never_becomes_an_upstream_header(slot, token):
    write_credentials(slot, token=token)
    code, raw, upstream = run(slot)
    assert code == 2 and response(raw)[0]["status"] == 401 and not upstream.calls


@pytest.mark.parametrize("target", ["credential", "profile", "config_parent"])
def test_authentication_symlinks_cannot_redirect_reads(slot, target):
    path = {"credential": slot / ".claude/.credentials.json", "profile": slot / ".claude.json",
            "config_parent": slot / ".config"}[target]
    path.rename(path.with_name(path.name + "-original"))
    path.symlink_to(path.with_name(path.name + "-original"))
    code, raw, upstream = run(slot)
    assert code == 2 and response(raw)[0]["status"] == 401 and not upstream.calls


def test_native_token_rotation_is_observed_without_writes_by_relay(slot):
    first = run(slot)[2]
    write_credentials(slot, token="new-native-slot-token")
    path = slot / ".claude/.credentials.json"
    stamp = path.stat().st_mtime_ns
    second = run(slot)[2]
    assert first.calls[0][1]["headers"]["authorization"] == "Bearer slot-test-token"
    assert second.calls[0][1]["headers"]["authorization"] == "Bearer new-native-slot-token"
    assert path.stat().st_mtime_ns == stamp


@pytest.mark.parametrize("change", ["renewed", "expired", "missing", "malformed"])
def test_credential_is_reread_after_connect_before_the_only_post(slot, change):
    upstream = Upstream()

    def connect():
        path = slot / ".claude/.credentials.json"
        if change == "renewed":
            write_credentials(slot, token="rotated-during-connect")
        elif change == "expired":
            write_credentials(slot, expiry=(time.time() - 1) * 1000)
        elif change == "missing":
            path.unlink()
        else:
            path.write_text("{bad")

    upstream.connect = connect
    code, raw, _ = run(slot, upstream=upstream)
    if change == "renewed":
        assert code == 0 and len(upstream.calls) == 1
        assert upstream.calls[0][1]["headers"]["authorization"] == "Bearer rotated-during-connect"
    else:
        assert code == 2 and response(raw)[0]["status"] == 401
        assert upstream.calls == []


@pytest.mark.parametrize("existing_stream", [True, False], ids=["existing-stream", "new-request"])
@pytest.mark.parametrize("position", [-1, 0, 1], ids=["just-before", "exactly-at", "just-after"])
def test_existing_stream_uses_zero_expiry_margin_but_new_requests_keep_thirty_seconds(
        slot, existing_stream, position):
    # An integral millisecond fixture round-trips exactly. time.time()*1000
    # can round upward, placing the stored expiry after the purported boundary.
    expiry_seconds = 1_700_000_040
    write_credentials(slot, expiry=expiry_seconds * 1000)
    boundary = float(expiry_seconds - (0 if existing_stream else 30))
    now = (math.nextafter(boundary, -math.inf) if position < 0 else
           math.nextafter(boundary, math.inf) if position > 0 else boundary)
    options = {"minimum_remaining": 0} if existing_stream else {}
    if position < 0:
        assert relay.credential(slot, now, **options) == ("slot-test-token", float(expiry_seconds))
    else:
        with pytest.raises(relay.RelayError) as error:
            relay.credential(slot, now, **options)
        assert error.value.status == 401


@pytest.mark.parametrize("account", ["", "\ud800", "bad\naccount", "x" * 257])
def test_corrupt_native_account_metadata_fails_with_safe_framed_error(slot, account):
    (slot / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": account}}))
    code, raw, upstream = run(slot)
    assert code == 2 and response(raw)[0]["status"] == 401
    assert not upstream.calls


def test_account_switch_during_connect_stops_before_any_post(slot):
    upstream = Upstream()

    def connect():
        (slot / ".claude.json").write_text('{"oauthAccount":{"accountUuid":"other"}}')

    upstream.connect = connect
    code, raw, upstream = run(slot, upstream=upstream)
    assert code == 2 and response(raw)[0]["status"] == 409
    assert not upstream.calls and upstream.closed


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 429, 500])
def test_redirects_and_api_errors_are_normalized_once_without_retry(slot, status):
    upstream = Upstream(status=status, headers=[("content-type", "application/json"),
                                                ("location", "https://must-not-follow.invalid"),
                                                ("set-cookie", "private-cookie")])
    code, raw, upstream = run(slot, upstream=upstream)
    meta, _ = response(raw)
    assert code == 2 and meta["status"] == (502 if status < 400 else status)
    assert meta["headers"] == {"content-type": "application/json"}
    assert len(upstream.calls) == 1
    assert upstream.read_sizes == []


@pytest.mark.parametrize("status,category", [
    (400, "invalid_request_error"), (401, "authentication_error"), (403, "permission_error"),
    (404, "not_found_error"), (408, "api_error"), (409, "invalid_request_error"),
    (413, "request_too_large"), (422, "invalid_request_error"), (429, "rate_limit_error"),
    (500, "api_error"), (503, "api_error"), (529, "overloaded_error"),
])
@pytest.mark.parametrize("private_body", [
    b'{"error":{"message":"slot-test-token owner-a private@example.invalid"}}',
    b'<html>slot-test-token owner-a private@example.invalid</html>',
    b'{"error":{"message":"malformed slot-test-token',
    b'{"error":{"message":"' + b"slot-test-token " * 5000 + b'"}}',
])
def test_provider_errors_never_echo_private_body_or_request_id(slot, capsys, status,
                                                              category, private_body):
    upstream = Upstream(status=status, chunks=[private_body], headers=[
        ("content-type", "application/json; secret=slot-test-token"),
        ("request-id", "req_slot-test-token"), ("retry-after", "000012"),
        ("set-cookie", "slot-test-token"), ("x-private", "private@example.invalid")])
    code, raw, upstream = run(slot, upstream=upstream)
    meta, body = response(raw)
    assert code == 2 and meta["status"] == status
    assert meta["headers"] == {"content-type": "application/json", "retry-after": "12"}
    answer = json.loads(body)
    assert answer["type"] == "error" and answer["error"]["type"] == category
    assert len(body) < 512 and len(upstream.calls) == 1 and upstream.closed
    assert upstream.read_bytes <= relay.MAX_ERROR_BODY
    if status != 400:
        assert upstream.read_sizes == []
    for secret in (b"slot-test-token", b"owner-a", b"private@example.invalid", b"<html>"):
        assert secret not in raw
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("error,overflow", [
    ({"type": "invalid_request_error", "message": "prompt is too long: slot-test-token"}, True),
    ({"type": "invalid_request_error", "message": "prompt is too long"}, True),
    ({"type": "context_window_exceeded", "message": "private@example.invalid"}, True),
    ({"type": "invalid_request_error", "message": "prompt is too longPRIVATE"}, False),
    ({"type": "untrusted_error", "message": "prompt is too long: slot-test-token"}, False),
    ({"type": "invalid_request_error", "message": "other slot-test-token"}, False),
    ({"type": "invalid_request_error", "message": 123}, False),
])
def test_context_overflow_retains_native_recovery_prefix_not_provider_text(slot, error, overflow):
    upstream = Upstream(status=400, chunks=[json.dumps({"error": error}).encode()],
                        headers=[("content-type", "application/json")])
    code, raw, _ = run(slot, upstream=upstream)
    meta, body = response(raw)
    answer = json.loads(body)
    assert code == 2 and meta["status"] == 400
    assert answer["error"]["type"] == "invalid_request_error"
    assert answer["error"]["message"].startswith("prompt is too long") is overflow
    assert b"slot-test-token" not in raw and b"private@example.invalid" not in raw
    assert len(upstream.calls) == 1


@pytest.mark.parametrize("mode", ["html", "known_oversize", "unknown_oversize", "truncated",
                                  "incomplete_length",
                                  "duplicate", "malformed", "not_object"])
def test_untrusted_error_classification_is_bounded_and_fails_to_generic(slot, mode):
    error = b'{"error":{"type":"invalid_request_error","message":"prompt is too long"}}'
    headers = [("content-type", "text/html" if mode == "html" else "application/json")]
    if mode.endswith("oversize"):
        error = b" " * relay.MAX_ERROR_BODY + error
    elif mode == "duplicate":
        error = b'{"error":{"type":"invalid_request_error","type":"context_window_exceeded"}}'
    elif mode == "malformed":
        error = b'{"error":"unfinished'
    elif mode == "not_object":
        error = b'[]'
    upstream = Upstream(status=400, chunks=[error], headers=headers,
                        failure="truncated" if mode == "truncated" else None)
    if mode == "known_oversize":
        upstream.length = len(error)
    elif mode == "incomplete_length":
        upstream.length = len(error) + 10
    code, raw, upstream = run(slot, upstream=upstream)
    meta, body = response(raw)
    assert code == 2 and meta["status"] == 400 and len(upstream.calls) == 1
    assert upstream.read_bytes <= relay.MAX_ERROR_BODY
    assert not json.loads(body)["error"]["message"].startswith("prompt is too long")
    if mode in {"html", "known_oversize"}:
        assert upstream.read_sizes == []
    elif mode == "unknown_oversize":
        assert upstream.read_sizes == [relay.MAX_ERROR_BODY]


@pytest.mark.parametrize("value,expected", [
    ("0", "0"), ("000001", "1"), ("86400", "86400"), ("86401", None),
    ("1234567890123456", None), ("-1", None), ("1.5", None),
    ("Wed, 21 Oct 2015 07:28:00 GMT", None), ("slot-test-token", None), ("", None),
])
def test_retry_after_is_only_bounded_canonical_numeric_guidance(slot, value, expected):
    upstream = Upstream(status=429, headers=[("retry-after", value)])
    code, raw, _ = run(slot, upstream=upstream)
    meta, _ = response(raw)
    assert code == 2 and meta["status"] == 429
    assert meta["headers"].get("retry-after") == expected


def test_successful_stream_bytes_unchanged_but_opaque_response_identity_removed(slot):
    chunks = [b"event: message_start\n\ndata: {\"text\":\"literal slot-test-token\"}\n\n",
              b"data: \xff\n\n", b"event: message_stop\n\n"]
    upstream = Upstream(chunks=chunks, headers=[
        ("content-type", "text/event-stream; charset=utf-8; identity=private"),
        ("request-id", "private@example.invalid")])
    code, raw, _ = run(slot, upstream=upstream)
    meta, body = response(raw)
    assert code == 0 and body == b"".join(chunks)
    assert meta["headers"] == {"content-type": "text/event-stream"}
    assert b"private@example.invalid" not in raw and len(upstream.calls) == 1
    assert upstream.sock.timeout == relay.READ_TIMEOUT


def test_partial_provider_error_times_out_to_safe_category_without_retry(slot, monkeypatch):
    reader, writer = socket.socketpair()
    reply = http.client.HTTPResponse(reader)
    writer.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\n"
                   b"Content-Length: 1024\r\n\r\n{\"error\":")
    reply.begin()
    upstream = Upstream(status=400)
    upstream.sock = reader
    upstream.getresponse = lambda: reply
    monkeypatch.setattr(relay, "ERROR_READ_TIMEOUT", 0.05)
    started = time.monotonic()
    try:
        code, raw, _ = run(slot, upstream=upstream)
        meta, body = response(raw)
        assert code == 2 and meta["status"] == 400
        assert json.loads(body)["error"]["type"] == "invalid_request_error"
        assert time.monotonic() - started < 1
        assert reader.gettimeout() <= 0.05
        assert len(upstream.calls) == 1 and upstream.closed
    finally:
        reply.close()
        reader.close()
        writer.close()


def test_dripping_error_body_has_total_deadline_not_reset_per_chunk(monkeypatch):
    now = [0.0]
    upstream = Upstream(status=400)
    calls = []

    def drip(size):
        calls.append(size)
        now[0] += 2
        return b" "

    upstream.read1 = drip
    monkeypatch.setattr(relay.time, "monotonic", lambda: now[0])
    assert not relay.context_overflow(upstream, {"content-type": "application/json"},
                                      upstream.sock)
    assert len(calls) == 3 and upstream.sock.timeout == 1


@pytest.mark.parametrize("headers", [[("content-type", "one"), ("Content-Type", "two")],
                                      [("retry-after", "1\nprivate")]])
def test_upstream_header_ambiguity_is_not_sent_to_local_client(slot, headers):
    code, raw, _ = run(slot, upstream=Upstream(headers=headers))
    assert code == 2 and response(raw)[0]["status"] == 502


@pytest.mark.parametrize("failure", ["connect", "response"])
def test_upstream_failure_is_safe_and_never_retried(slot, failure):
    code, raw, upstream = run(slot, upstream=Upstream(failure=failure))
    assert code == 2 and response(raw)[0]["status"] == 502
    assert len(upstream.calls) == (0 if failure == "connect" else 1)
    assert b"private" not in raw and b"slot-test-token" not in raw


def test_truncated_sse_has_no_success_terminator(slot):
    code, raw, upstream = run(slot, upstream=Upstream(failure="truncated"))
    assert code == 2 and len(upstream.calls) == 1 and upstream.closed
    with pytest.raises(EOFError):
        response(raw)


def test_eof_before_declared_content_length_is_not_success(slot):
    upstream = Upstream()
    upstream.length = 10
    code, raw, _ = run(slot, upstream=upstream)
    assert code == 2
    with pytest.raises(EOFError):
        response(raw)


@pytest.mark.parametrize("status", [True, 0, 101, 199, 600])
def test_invalid_or_informational_upstream_status_is_not_a_final_response(slot, status):
    code, raw, upstream = run(slot, upstream=Upstream(status=status))
    assert code == 2 and response(raw)[0]["status"] == 502
    assert len(upstream.calls) == 1


@pytest.mark.parametrize("reason", ["disconnect", "policy", "account", "deadline", "expiry",
                                    "credential_missing", "credential_malformed"])
def test_inflight_stream_is_cancelled_on_revocation_account_change_or_timeout(slot, monkeypatch, reason):
    read_side, write_side = socket.socketpair()
    source = read_side.makefile("rb")
    entered = threading.Event()
    allowed = threading.Event()
    allowed.set()
    upstream = Upstream()
    output = io.BytesIO()
    result = []

    def policy():
        if not allowed.is_set():
            raise relay.RelayError(403, "disabled")

    def read(size):
        entered.set()
        assert upstream.sock.cancelled.wait(3)
        return b""

    upstream.read1 = read
    if reason == "deadline":
        monkeypatch.setattr(relay, "REQUEST_TIMEOUT", 0.01)
    worker = threading.Thread(target=lambda: result.append(relay.serve_one(
        source, output, slot, policy=policy, connect=lambda: upstream, watch=True)))
    write_side.sendall(request())
    worker.start()
    try:
        assert entered.wait(2)
        if reason == "disconnect":
            write_side.shutdown(socket.SHUT_WR)
        elif reason == "policy":
            allowed.clear()
        elif reason == "account":
            (slot / ".claude.json").write_text('{"oauthAccount":{"accountUuid":"other"}}')
        elif reason == "expiry":
            now = time.time()
            monkeypatch.setattr(relay.time, "time", lambda: now + 7200)
        elif reason == "credential_missing":
            (slot / ".claude/.credentials.json").unlink()
        elif reason == "credential_malformed":
            (slot / ".claude/.credentials.json").write_text("{bad")
        worker.join(timeout=4)
        assert not worker.is_alive() and result == [2]
        assert upstream.closed and len(upstream.calls) == 1
        with pytest.raises(EOFError):
            response(output.getvalue())
    finally:
        write_side.close()
        source.close()
        read_side.close()


def test_same_account_native_renewal_preserves_stream_past_original_expiry(slot, monkeypatch):
    now = [time.time()]
    old_expiry = now[0] + 60
    write_credentials(slot, expiry=old_expiry * 1000)
    monkeypatch.setattr(relay.time, "time", lambda: now[0])
    checked = threading.Event()
    original_credential = relay.credential

    def credential(*args, **kwargs):
        result = original_credential(*args, **kwargs)
        if kwargs.get("minimum_remaining") == 0 and now[0] > old_expiry:
            checked.set()
        return result

    monkeypatch.setattr(relay, "credential", credential)
    read_side, write_side = socket.socketpair()
    source = read_side.makefile("rb")
    entered, release = threading.Event(), threading.Event()
    upstream, output, result = Upstream(), io.BytesIO(), []
    original_read = upstream.read1

    def read(size):
        entered.set()
        assert release.wait(3)
        return original_read(size)

    upstream.read1 = read
    worker = threading.Thread(target=lambda: result.append(relay.serve_one(
        source, output, slot, policy=lambda: None, connect=lambda: upstream, watch=True)))
    write_side.sendall(request())
    worker.start()
    try:
        assert entered.wait(2)
        # Native writers replace atomically: no transient malformed file is part
        # of this test of a valid same-account renewal.
        target = slot / ".claude/.credentials.json"
        temporary = target.with_suffix(".new")
        temporary.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "renewed-native-token", "expiresAt": (old_expiry + 3600) * 1000}}))
        temporary.replace(target)
        now[0] = old_expiry + 10
        assert checked.wait(2)
        assert not upstream.sock.cancelled.is_set()
        release.set()
        worker.join(timeout=3)
        assert not worker.is_alive() and result == [0]
        assert response(output.getvalue())[0]["status"] == 200
        assert len(upstream.calls) == 1
        assert upstream.calls[0][1]["headers"]["authorization"] == "Bearer slot-test-token"
        assert b"renewed-native-token" not in output.getvalue()
    finally:
        release.set()
        write_side.close()
        worker.join(timeout=3)
        source.close()
        read_side.close()


def test_connection_close_response_keeps_cancellation_socket_while_read_is_blocked(slot):
    read_side, write_side = socket.socketpair()
    source = read_side.makefile("rb")
    server, peer = socket.socketpair()
    reply = http.client.HTTPResponse(server)
    peer.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                 b"Connection: close\r\n\r\n")
    reply.begin()
    upstream, output, result = Upstream(), io.BytesIO(), []
    upstream.sock = server
    entered, allowed = threading.Event(), threading.Event()
    allowed.set()

    def policy():
        if not allowed.is_set():
            raise relay.RelayError(403, "disabled")

    def getresponse():
        # HTTPConnection releases its socket reference for will_close responses;
        # the HTTPResponse file still owns that live, blocking stream.
        assert reply.will_close
        server.close()
        upstream.sock = None
        return reply

    original_read = reply.read1

    def read(size):
        entered.set()
        return original_read(size)

    reply.read1 = read
    upstream.getresponse = getresponse
    worker = threading.Thread(target=lambda: result.append(relay.serve_one(
        source, output, slot, policy=policy, connect=lambda: upstream, watch=True)))
    write_side.sendall(request())
    worker.start()
    try:
        assert entered.wait(2)
        allowed.clear()
        worker.join(timeout=2)
        assert not worker.is_alive() and result == [2]
        assert len(upstream.calls) == 1 and upstream.closed
        with pytest.raises(EOFError):
            response(output.getvalue())
    finally:
        peer.close()
        worker.join(timeout=3)
        reply.close()
        server.close()
        write_side.close()
        source.close()
        read_side.close()


def test_cancelled_buffered_chunk_is_not_forwarded_after_blocking_read(slot):
    read_side, write_side = socket.socketpair()
    source = read_side.makefile("rb")
    upstream, output, result = Upstream(), io.BytesIO(), []
    entered = threading.Event()

    def read(size):
        entered.set()
        assert upstream.sock.cancelled.wait(3)
        return b"buffered-after-cancellation"

    upstream.read1 = read
    worker = threading.Thread(target=lambda: result.append(relay.serve_one(
        source, output, slot, policy=lambda: None, connect=lambda: upstream, watch=True)))
    write_side.sendall(request())
    worker.start()
    try:
        assert entered.wait(2)
        write_side.shutdown(socket.SHUT_WR)
        worker.join(timeout=3)
        assert not worker.is_alive() and result == [2]
        assert b"buffered-after-cancellation" not in output.getvalue()
        with pytest.raises(EOFError):
            response(output.getvalue())
    finally:
        write_side.close()
        worker.join(timeout=3)
        source.close()
        read_side.close()


def test_upstream_host_port_and_certificate_verification_are_fixed(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
                 "all_proxy", "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS"):
        monkeypatch.setenv(name, "https://untrusted-routing.invalid")
    captured = []
    monkeypatch.setattr(relay.http.client, "HTTPSConnection", lambda *args, **kwargs:
                        captured.append((args, kwargs)))
    relay.connect_upstream()
    args, kwargs = captured[0]
    assert args == ("api.anthropic.com", 443)
    assert kwargs["context"].check_hostname is True
    assert kwargs["context"].verify_mode == relay.ssl.CERT_REQUIRED


def test_v2_main_uses_authenticated_unix_home_not_forwarded_environment(monkeypatch):
    monkeypatch.setenv("HOME", "/untrusted-client-home")
    monkeypatch.setattr(relay.pwd, "getpwuid", lambda _: SimpleNamespace(pw_dir="/slot-home"))
    monkeypatch.setattr(relay.signal, "signal", lambda *a: None)
    alarms = []
    monkeypatch.setattr(relay.signal, "alarm", alarms.append)
    monkeypatch.setattr(relay.sys, "stdin", SimpleNamespace(buffer=io.BytesIO()))
    monkeypatch.setattr(relay.sys, "stdout", SimpleNamespace(buffer=io.BytesIO()))
    calls = []

    def serve(input_, output, home, **kwargs):
        calls.append((home, kwargs["watch"]))
        kwargs["request_ready"]()
        return 0

    monkeypatch.setattr(relay, "serve_one", serve)
    assert relay.main(["--protocol-v2"]) == 0
    assert calls == [(Path("/slot-home"), True)]
    assert alarms == [relay.INPUT_TIMEOUT, relay.REQUEST_TIMEOUT, 0]


@pytest.mark.parametrize("command,accepted", [
    ("ccfleet-inference-v1", True), ("ccfleet-relay-v1", False),
    ("ccfleet-inference-v1 extra", False), ("ccfleet-inference-v1\nid", False),
    ("ccfleet-inference-v1; id", False),
])
def test_only_exact_new_forced_command_can_enter_v2(tmp_path, command, accepted):
    entry = tmp_path / "slot-entry.sh"
    entry.write_text((Path(__file__).resolve().parents[1] / "node/slot-entry.sh").read_text())
    package = tmp_path / "ccfleet_agent"
    package.mkdir()
    (package / "local_relay.py").write_text("import sys\nprint(repr(sys.argv[1:]))\n")
    result = subprocess.run(["bash", str(entry)], capture_output=True, text=True,
                            env={**os.environ, "SSH_ORIGINAL_COMMAND": command})
    assert (result.returncode == 0) == accepted
    assert result.stdout.strip() == ("['--protocol-v2']" if accepted else "")
