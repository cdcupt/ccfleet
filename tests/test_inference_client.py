"""Real loopback HTTP and synthetic SSH peers, without Claude/model calls."""

from __future__ import annotations

import http.client
import io
import json
import socket
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ccfleet_agent import inference_client as relay

PEER = r'''
import json, os, struct, sys, time
from pathlib import Path
mode, capture, gate = sys.argv[1:4]
if mode == "blocked-upload":
    Path(capture).write_text(json.dumps({"pid": os.getpid()}))
    time.sleep(10)
    raise SystemExit(2)
def exact(size):
    data = b""
    while len(data) < size:
        part = sys.stdin.buffer.read(size - len(data))
        if not part:
            raise EOFError()
        data += part
    return data
def frame(data):
    sys.stdout.buffer.write(struct.pack("!I", len(data)) + data)
    sys.stdout.buffer.flush()
meta = json.loads(exact(struct.unpack("!I", exact(4))[0]))
body = exact(meta.get("body_size", 0))
Path(capture).write_text(json.dumps({"meta": meta, "body": body.decode(), "pid": os.getpid()}))
if mode == "hang":
    sys.stdin.buffer.read()
    raise SystemExit(0)
status = 403 if mode == "forbidden" else 302 if mode == "redirect" else 200
headers = {"content-type": "text/event-stream"}
if mode == "bad-header":
    headers["location"] = "https://must-not-follow.invalid"
frame(json.dumps({"version": 2, "status": status, "headers": headers}).encode())
if mode == "oversized":
    sys.stdout.buffer.write(struct.pack("!I", 65537))
    sys.stdout.buffer.flush()
    raise SystemExit(0)
if meta.get("operation") == "status":
    frame(json.dumps({"ready": True, "protocol": 2}).encode())
else:
    frame(b"data: first\n\n")
    if mode == "stream":
        deadline = time.monotonic() + 5
        while not Path(gate).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not Path(gate).exists():
            raise SystemExit(2)
        frame(b"data: second\n\n")
    if mode == "idle-stream":
        sys.stdin.buffer.read()
        raise SystemExit(2)
if mode == "truncated":
    raise SystemExit(2)
frame(b"")
if mode == "trailing":
    sys.stdout.buffer.write(b"unexpected")
    sys.stdout.buffer.flush()
raise SystemExit(2 if status >= 400 else 0)
'''


@pytest.fixture
def peer(tmp_path):
    script = tmp_path / "synthetic-ssh.py"
    script.write_text(PEER)
    capture, gate = tmp_path / "capture.json", tmp_path / "continue"
    calls = []

    def command(mode="ok"):
        def result():
            calls.append(mode)
            return [sys.executable, "-I", str(script), mode, str(capture), str(gate),
                    "ccfleet-inference-v1"]
        return result

    return command, capture, gate, calls


@pytest.fixture
def bridge(peer):
    command, _, _, _ = peer
    with relay.Bridge(command(), request_timeout=5) as value:
        yield value


def post(bridge, body=None, headers=None, path="/v1/messages"):
    body = json.dumps(body or {"model": "test", "messages": []}).encode()
    fields = {"Authorization": "Bearer " + bridge.secret, "Content-Type": "application/json"}
    fields.update(headers or {})
    connection = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=3)
    connection.request("POST", path, body=body, headers=fields)
    return connection, connection.getresponse()


def raw_request(bridge, fields, *, method="POST", path="/v1/messages", body=b"{}"):
    request = (f"{method} {path} HTTP/1.1\r\n".encode()
               + b"\r\n".join(name.encode() + b": " + value.encode() for name, value in fields)
               + b"\r\n\r\n" + body)
    with socket.create_connection(("127.0.0.1", bridge.port), timeout=3) as connection:
        connection.sendall(request)
        response = http.client.HTTPResponse(connection, method=method)
        response.begin()
        return response.status, response.read()


def ordinary_headers(bridge):
    return [("Host", f"127.0.0.1:{bridge.port}"),
            ("Authorization", "Bearer " + bridge.secret), ("Content-Length", "2"),
            ("Content-Type", "application/json")]


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail("loopback bridge test deadline expired")


def test_loopback_nonce_lifetime_and_fixed_ssh_command(peer):
    command, _, _, _ = peer
    with relay.Bridge(command()) as first, relay.Bridge(command()) as second:
        assert first.server.server_address[0] == "127.0.0.1"
        assert first.base_url == f"http://127.0.0.1:{first.port}"
        assert first.secret != second.secret and len(first.secret) >= 32
    with pytest.raises(relay.RelayError, match="fixed"):
        relay.check_status(lambda: ["ssh", "arbitrary-command"])


def test_status_preflight_uses_v2_and_never_sends_model_body(peer):
    command, capture, _, calls = peer
    assert relay.check_status(command()) == {"ready": True, "protocol": 2}
    saved = json.loads(capture.read_text())
    assert saved["meta"] == {"version": 2, "operation": "status"}
    assert saved["body"] == "" and calls == ["ok"]


def test_status_errors_and_timeouts_are_sanitized(peer):
    command, _, _, _ = peer
    with pytest.raises(relay.RelayError) as error:
        relay.check_status(command("forbidden"))
    assert error.value.status == 403
    assert "not enabled" in str(error.value)
    with pytest.raises(relay.RelayError):
        relay.check_status(command("hang"), timeout=0.15)


def test_headers_and_structured_identity_are_removed_before_ssh(bridge, peer, capsys):
    _, capture, _, calls = peer
    messages = [{"role": "user", "content": "Literal user_id in source is not rewritten."}]
    body = {"model": "test", "messages": messages, "system": "local/path is explicit content",
            "tools": [{"name": "tool", "input_schema": {}}],
            **{key: {"private": "LOCAL-IDENTITY-SENTINEL"} for key in relay.IDENTITY_FIELDS}}
    connection, response = post(bridge, body, {
        "User-Agent": "PRIVATE-LOCAL-CLI", "x-stainless-os": "PRIVATE-OS",
        "X-Forwarded-For": "PRIVATE-IP", "Cookie": "PRIVATE-COOKIE",
        "anthropic-version": "2023-06-01", "anthropic-beta": "some-beta",
        "Accept": "text/event-stream"})
    assert response.status == 200 and response.read() == b"data: first\n\n"
    connection.close()
    saved = json.loads(capture.read_text())
    assert set(saved["meta"]["headers"]) == relay.REQUEST_HEADERS
    sent = json.loads(saved["body"])
    assert sent == {name: value for name, value in body.items() if name not in relay.IDENTITY_FIELDS}
    assert sent["messages"] == messages
    for secret in (bridge.secret, "LOCAL-IDENTITY-SENTINEL", "PRIVATE-LOCAL-CLI",
                   "PRIVATE-OS", "PRIVATE-IP", "PRIVATE-COOKIE"):
        assert secret not in capture.read_text()
        assert secret not in capsys.readouterr().out
    assert calls == ["ok"]


@pytest.mark.parametrize("path", sorted(relay.PATHS))
def test_semantic_header_parameters_are_removed_before_ssh(bridge, peer, capsys, path):
    context = {"model": "test", "messages": [], "system": "Native local/path stays intact"}
    connection, response = post(bridge, context, {
        "Content-Type": 'application/json; charset=utf-8; device="PRIVATE-DEVICE"',
        "Accept": 'text/event-stream; environment="PRIVATE-OS,LOCATION";q=0.900, '
                  'application/json; fingerprint=PRIVATE-FINGERPRINT;q=1.000',
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20, future-feature-2027-01-01",
    }, path=path)
    assert response.status == 200 and response.read() == b"data: first\n\n"
    connection.close()
    capture = peer[1].read_text()
    saved = json.loads(capture)
    assert saved["meta"]["headers"] == {
        "content-type": "application/json",
        "accept": "text/event-stream;q=0.9, application/json;q=1",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20,future-feature-2027-01-01",
    }
    assert json.loads(saved["body"]) == context
    assert "PRIVATE-" not in capture and peer[3] == ["ok"]
    output = capsys.readouterr()
    assert "PRIVATE-" not in output.out + output.err


@pytest.mark.parametrize("name, value", [
    ("Content-Type", "text/plain; device=PRIVATE-DEVICE"),
    ("Accept", "application/json;q=PRIVATE-LOCATION"),
    ("Accept", 'text/event-stream; device="PRIVATE-DEVICE'),
    ("Accept", "application/json;q=0.2;q=0.3"),
    ("anthropic-version", "2023-06-01; environment=PRIVATE-OS"),
    ("anthropic-version", "2023-02-29"),
    ("anthropic-beta", "native-feature; fingerprint=PRIVATE-FINGERPRINT"),
    ("anthropic-beta", ",".join(["feature"] * 65)),
])
def test_invalid_semantic_header_values_never_start_ssh(bridge, peer, capsys, name, value):
    connection, response = post(bridge, headers={name: value})
    assert response.status == 400
    assert json.loads(response.read())["error"]["message"] == "invalid model request headers"
    connection.close()
    assert peer[3] == [] and not peer[1].exists()
    output = capsys.readouterr()
    assert "PRIVATE-" not in output.out + output.err


def test_bare_native_api_key_nonce_auth_is_supported_without_forwarding_it(bridge, peer):
    fields = [("Host", f"127.0.0.1:{bridge.port}"), ("x-api-key", bridge.secret),
              ("Content-Length", "2"), ("Content-Type", "application/json")]
    status, body = raw_request(bridge, fields)
    assert status == 200 and body == b"data: first\n\n"
    assert bridge.secret not in peer[1].read_text()


@pytest.mark.parametrize("change, expected", [
    ("missing-auth", 401), ("wrong-auth", 401), ("both-auth", 401),
    ("duplicate-auth", 401), ("duplicate-key", 401), ("wrong-host", 403),
    ("duplicate-host", 403), ("origin", 403), ("empty-origin", 403),
    ("duplicate-length", 400), ("chunked", 400), ("empty-transfer", 400),
    ("leading-zero", 400), ("oversized", 413), ("duplicate-semantic", 400),
    ("folded", 400),
])
def test_ambiguous_or_unauthorized_http_never_starts_ssh(bridge, peer, change, expected):
    fields = ordinary_headers(bridge)
    edits = {
        "both-auth": [("x-api-key", bridge.secret)],
        "duplicate-auth": [("Authorization", "Bearer " + bridge.secret)],
        "duplicate-key": [("x-api-key", bridge.secret), ("x-api-key", bridge.secret)],
        "duplicate-host": [("Host", "bad.invalid")], "origin": [("Origin", "https://bad.invalid")],
        "empty-origin": [("Origin", "")], "duplicate-length": [("Content-Length", "2")],
        "chunked": [("Transfer-Encoding", "chunked")], "empty-transfer": [("Transfer-Encoding", "")],
        "duplicate-semantic": [("Accept", "a"), ("accept", "b")],
        "folded": [("anthropic-beta", "first\r\n second")],
    }
    fields += edits.get(change, [])
    if change == "missing-auth":
        fields = [field for field in fields if field[0] != "Authorization"]
    for trigger, name, value in (("wrong-auth", "Authorization", "Bearer private-wrong"),
                                 ("wrong-host", "Host", "bad.invalid"),
                                 ("leading-zero", "Content-Length", "02"),
                                 ("oversized", "Content-Length", str(relay.MAX_BODY + 1))):
        if change == trigger:
            fields = [(key, value if key == name else previous) for key, previous in fields]
    assert raw_request(bridge, fields)[0] == expected
    assert peer[3] == []


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "PUT", "DELETE", "CONNECT"])
def test_other_http_methods_are_rejected(bridge, peer, method):
    assert raw_request(bridge, ordinary_headers(bridge), method=method)[0] == 405
    assert peer[3] == []


@pytest.mark.parametrize("path", ["/", "/v1/models", "/v1/messages?other=true",
                                 "http://elsewhere.invalid/v1/messages", "/v1/%6dessages"])
def test_arbitrary_paths_and_absolute_targets_are_rejected(bridge, peer, path):
    assert raw_request(bridge, ordinary_headers(bridge), path=path)[0] == 400
    assert peer[3] == []


@pytest.mark.parametrize("path", sorted(relay.PATHS))
def test_only_fixed_model_and_count_token_paths_are_forwarded(bridge, peer, path):
    connection, response = post(bridge, path=path)
    assert response.status == 200
    response.read()
    connection.close()
    assert json.loads(peer[1].read_text())["meta"]["path"] == path


@pytest.mark.parametrize("raw", [b"[]", b"{bad", b'{"model":1,"model":2}', b'{"value":NaN}',
                                 b'{"value":Infinity}', b'{"value":"\xff"}'])
def test_invalid_json_and_duplicates_are_refused_before_ssh(bridge, peer, raw):
    fields = [(name, str(len(raw)) if name == "Content-Length" else value)
              for name, value in ordinary_headers(bridge)]
    assert raw_request(bridge, fields, body=raw)[0] == 400
    assert peer[3] == []


def test_sse_delivers_first_frame_before_later_frames_exist(peer):
    command, _, gate, _ = peer
    with relay.Bridge(command("stream"), request_timeout=5) as bridge:
        connection, response = post(bridge)
        assert response.status == 200
        assert response.read(len(b"data: first\n\n")) == b"data: first\n\n"
        gate.touch()
        assert response.read() == b"data: second\n\n"
        connection.close()


@pytest.mark.parametrize("mode", ["truncated", "oversized", "trailing"])
def test_truncated_or_invalid_stream_has_no_fabricated_success_terminator(peer, mode):
    command, _, _, calls = peer
    with relay.Bridge(command(mode), request_timeout=3) as bridge:
        connection, response = post(bridge)
        assert response.status == 200
        with pytest.raises(http.client.IncompleteRead):
            response.read()
        connection.close()
    assert calls == [mode]


@pytest.mark.parametrize("mode", ["redirect", "bad-header"])
def test_redirect_and_unsafe_response_header_are_rejected_before_streaming(peer, mode):
    command, _, _, _ = peer
    with relay.Bridge(command(mode), request_timeout=3) as bridge:
        connection, response = post(bridge)
        assert response.status == 502
        assert response.getheader("location") is None
        response.read()
        connection.close()


def test_client_disconnect_cancels_blocked_ssh_stream(peer):
    command, capture, _, _ = peer
    with relay.Bridge(command("idle-stream"), request_timeout=10) as bridge:
        connection, response = post(bridge)
        assert response.read(len(b"data: first\n\n")) == b"data: first\n\n"
        wait_until(lambda: capture.exists() and bool(bridge.server.processes))
        children = list(bridge.server.processes)
        response.close()
        connection.close()
        wait_until(lambda: all(child.poll() is not None for child in children))
        wait_until(lambda: not bridge.server.processes)


def test_client_disconnect_cancels_blocked_ssh_upload_before_request_deadline(peer):
    command, capture, _, calls = peer
    # Much larger than the OS pipe buffer: the synthetic SSH peer deliberately
    # never reads stdin, so the bridge must cancel while still writing the body.
    body = json.dumps({"messages": [{"role": "user", "content": "x" * 1024 * 1024}]}).encode()
    with relay.Bridge(command("blocked-upload"), request_timeout=10) as bridge:
        connection = socket.create_connection(("127.0.0.1", bridge.port), timeout=3)
        try:
            prefix = (f"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1:{bridge.port}\r\n"
                      f"Authorization: Bearer {bridge.secret}\r\nContent-Type: application/json\r\n"
                      f"Content-Length: {len(body)}\r\n\r\n").encode()
            connection.sendall(prefix + body)
            wait_until(lambda: capture.exists() and bool(bridge.server.processes))
            children = list(bridge.server.processes)
            assert all(child.poll() is None for child in children)
            connection.close()
            wait_until(lambda: all(child.poll() is not None for child in children))
            wait_until(lambda: not bridge.server.processes)
            assert calls == ["blocked-upload"]
        finally:
            connection.close()


def test_close_cancels_all_active_children_and_request_deadline_is_bounded(peer):
    command, _, _, _ = peer
    with relay.Bridge(command("hang"), request_timeout=0.15) as bridge:
        connection, response = post(bridge)
        assert response.status == 502
        response.read()
        connection.close()
        wait_until(lambda: not bridge.server.processes)
    bridge = relay.Bridge(command("idle-stream"), request_timeout=10).start()
    connection, response = post(bridge)
    response.read(len(b"data: first\n\n"))
    children = list(bridge.server.processes)
    bridge.close()
    wait_until(lambda: all(child.poll() is not None for child in children))
    response.close()
    connection.close()


def test_concurrency_is_resource_bounded_before_starting_extra_ssh(peer):
    command, _, _, calls = peer
    with relay.Bridge(command(), max_requests=1, max_pending=0, body_timeout=2) as bridge:
        first = socket.create_connection(("127.0.0.1", bridge.port), timeout=3)
        first.sendall((f"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1:{bridge.port}\r\n"
                       f"Authorization: Bearer {bridge.secret}\r\n"
                       "Content-Type: application/json\r\nContent-Length: 100\r\n\r\n"
                       "{").encode())
        try:
            wait_until(lambda: len(bridge.server.connections) == 1)
            # Admission rejects at accept(), before reading any HTTP bytes.
            # Sending headers then a body races the intentional close on Linux
            # and can legitimately raise BrokenPipeError in the test client.
            with socket.create_connection(("127.0.0.1", bridge.port), timeout=3) as connection:
                response = http.client.HTTPResponse(connection)
                try:
                    response.begin()
                    assert response.status == 503
                    assert response.getheader("Connection") == "close"
                    assert response.getheader("Content-Type") == "application/json"
                    assert response.getheader("Retry-After") == "3"
                    body = response.read()
                    assert json.loads(body)["error"]["type"] == "overloaded_error"
                    assert b"not forwarded" in body and bridge.secret.encode() not in body
                finally:
                    response.close()
            assert calls == []
        finally:
            first.close()


def test_default_bridge_supports_parallel_native_agents_beyond_four(peer):
    command, _, gate, calls = peer
    connections = []
    with relay.Bridge(command("stream")) as bridge:
        try:
            for _ in range(5):
                connection, response = post(bridge)
                connections.append((connection, response))
                assert response.status == 200
                assert response.read(len(b"data: first\n\n")) == b"data: first\n\n"
            assert calls == ["stream"] * 5 and len(bridge.server.processes) == 5
            gate.touch()
            for _, response in connections:
                assert response.read() == b"data: second\n\n"
        finally:
            for connection, response in connections:
                response.close()
                connection.close()


def test_silent_preconnection_does_not_occupy_model_capacity(peer):
    command, _, _, calls = peer
    with relay.Bridge(command(), max_requests=1, max_pending=1) as bridge:
        with socket.create_connection(("127.0.0.1", bridge.port), timeout=3):
            wait_until(lambda: len(bridge.server.connections) == 1)
            connection, response = post(bridge)
            assert response.status == 200 and response.read() == b"data: first\n\n"
            connection.close()
            assert calls == ["ok"]


def test_idle_headers_expire_and_release_bounded_connection_capacity(peer):
    command, _, _, calls = peer
    with relay.Bridge(command(), max_requests=1, max_pending=0, header_timeout=0.15) as bridge:
        with socket.create_connection(("127.0.0.1", bridge.port), timeout=3) as idle:
            wait_until(lambda: len(bridge.server.connections) == 1)
            assert idle.recv(1) == b""
        wait_until(lambda: not bridge.server.connections)
        assert calls == []
        connection, response = post(bridge)
        assert response.status == 200 and response.read() == b"data: first\n\n"
        connection.close()


def test_short_parallel_burst_waits_without_replaying_or_exceeding_model_capacity(peer):
    command, _, gate, calls = peer
    with relay.Bridge(command("stream"), max_requests=1, max_pending=1) as bridge:
        first, response = post(bridge)
        assert response.read(len(b"data: first\n\n")) == b"data: first\n\n"
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(post, bridge)
            wait_until(lambda: len(bridge.server.connections) == 2)
            assert calls == ["stream"] and len(bridge.server.processes) == 1
            gate.touch()
            assert response.read() == b"data: second\n\n"
            first.close()
            second, next_response = pending.result(timeout=3)
            assert next_response.status == 200
            assert next_response.read() == b"data: first\n\ndata: second\n\n"
            second.close()
        assert calls == ["stream", "stream"]


def test_waiting_model_request_expires_with_safe_busy_json_and_does_not_start_ssh(peer):
    command, _, _, calls = peer
    with relay.Bridge(command("idle-stream"), max_requests=1, max_pending=1,
                      admission_timeout=0.15) as bridge:
        first, response = post(bridge)
        assert response.read(len(b"data: first\n\n")) == b"data: first\n\n"
        second, busy = post(bridge)
        assert busy.status == 503 and busy.getheader("Retry-After") == "3"
        body = busy.read()
        assert json.loads(body)["error"]["type"] == "overloaded_error"
        assert b"not forwarded" in body and bridge.secret.encode() not in body
        second.close()
        assert calls == ["idle-stream"] and len(bridge.server.processes) == 1
        response.close()
        first.close()
        wait_until(lambda: not bridge.server.processes and not bridge.server.connections)
        assert calls == ["idle-stream"]  # The expired request is never replayed.
        third, recovered = post(bridge)
        assert recovered.status == 200
        assert recovered.read(len(b"data: first\n\n")) == b"data: first\n\n"
        recovered.close()
        third.close()
        assert calls == ["idle-stream", "idle-stream"]


def test_invalid_auth_is_rejected_without_waiting_for_model_capacity(peer):
    command, _, _, calls = peer
    with relay.Bridge(command("idle-stream"), max_requests=1, max_pending=1) as bridge:
        first, response = post(bridge)
        assert response.read(len(b"data: first\n\n")) == b"data: first\n\n"
        second, denied = post(bridge, headers={"Authorization": "Bearer invalid"})
        assert denied.status == 401
        denied.read()
        second.close()
        assert calls == ["idle-stream"]
        response.close()
        first.close()


def test_invalid_json_releases_model_permit_before_the_next_request(peer):
    command, _, _, calls = peer
    with relay.Bridge(command(), max_requests=1, max_pending=1) as bridge:
        fields = [("Host", f"127.0.0.1:{bridge.port}"),
                  ("Authorization", "Bearer " + bridge.secret), ("Content-Length", "1"),
                  ("Content-Type", "application/json")]
        assert raw_request(bridge, fields, body=b"{")[0] == 400
        assert calls == []
        connection, response = post(bridge)
        assert response.status == 200 and response.read() == b"data: first\n\n"
        connection.close()
        assert calls == ["ok"]


def test_disconnected_pending_request_releases_connection_without_starting_ssh(peer):
    command, _, _, calls = peer
    with relay.Bridge(command("idle-stream"), max_requests=1, max_pending=1,
                      admission_timeout=30) as bridge:
        first, response = post(bridge)
        assert response.read(len(b"data: first\n\n")) == b"data: first\n\n"
        pending = socket.create_connection(("127.0.0.1", bridge.port), timeout=3)
        pending.sendall((f"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1:{bridge.port}\r\n"
                         f"Authorization: Bearer {bridge.secret}\r\nContent-Type: application/json\r\n"
                         "Content-Length: 2\r\n\r\n{}").encode())
        wait_until(lambda: len(bridge.server.connections) == 2)
        pending.close()
        wait_until(lambda: len(bridge.server.connections) == 1)
        assert calls == ["idle-stream"]
        response.close()
        first.close()


def test_queued_large_body_with_fin_is_cancelled_after_read_before_ssh_spawn(peer):
    command, _, _, calls = peer
    with relay.Bridge(command("idle-stream"), max_requests=1, max_pending=1,
                      admission_timeout=30) as bridge:
        first, response = post(bridge)
        assert response.read(len(b"data: first\n\n")) == b"data: first\n\n"
        body = json.dumps({"model": "test", "messages": [
            {"role": "user", "content": "x" * (128 * 1024)}]}).encode()
        pending = socket.create_connection(("127.0.0.1", bridge.port), timeout=3)
        try:
            pending.sendall((f"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1:{bridge.port}\r\n"
                             f"Authorization: Bearer {bridge.secret}\r\n"
                             "Content-Type: application/json\r\n"
                             f"Content-Length: {len(body)}\r\n\r\n").encode() + body)
            pending.shutdown(socket.SHUT_WR)
            wait_until(lambda: len(bridge.server.connections) == 2)
            # The unread body is larger than HTTP read-ahead, so MSG_PEEK still
            # sees body data instead of EOF while the sole model permit is busy.
            assert calls == ["idle-stream"]
            response.close()
            first.close()
            wait_until(lambda: not bridge.server.connections and not bridge.server.processes)
            assert calls == ["idle-stream"]
            recovered, ready = post(bridge)
            assert ready.status == 200
            assert ready.read(len(b"data: first\n\n")) == b"data: first\n\n"
            ready.close()
            recovered.close()
            assert calls == ["idle-stream", "idle-stream"]
        finally:
            pending.close()
            response.close()
            first.close()


def test_large_upload_rejection_is_bounded_and_never_forwarded_even_if_tcp_resets(peer):
    command, _, _, calls = peer
    with relay.Bridge(command("idle-stream"), max_requests=1, max_pending=0) as bridge:
        first, response = post(bridge)
        assert response.read(len(b"data: first\n\n")) == b"data: first\n\n"
        overloaded = socket.create_connection(("127.0.0.1", bridge.port), timeout=2)
        sent = threading.Event()

        def upload():
            try:
                overloaded.sendall((f"POST /v1/messages HTTP/1.1\r\n"
                                    f"Host: 127.0.0.1:{bridge.port}\r\n"
                                    f"Authorization: Bearer {bridge.secret}\r\n"
                                    "Content-Type: application/json\r\n"
                                    "Content-Length: 4194304\r\n\r\n").encode())
                for _ in range(64):
                    overloaded.sendall(b"x" * 65536)
            except OSError:
                pass  # Early refusal can reset a connection with unread upload bytes.
            finally:
                sent.set()

        sender = threading.Thread(target=upload)
        sender.start()
        raw = bytearray()
        began = time.monotonic()
        try:
            while True:
                try:
                    part = overloaded.recv(4096)
                except OSError:
                    break
                if not part:
                    break
                raw.extend(part)
                assert len(raw) <= 1024
            assert sent.wait(3) and time.monotonic() - began < 3
            if raw:
                status_line = b"HTTP/1.1 503 Service Unavailable\r\n"
                assert status_line.startswith(bytes(raw[:len(status_line)]))
                headers, separator, body = bytes(raw).partition(b"\r\n\r\n")
                if separator:
                    assert b"Retry-After: 3\r\n" in headers + b"\r\n"
                    length = int(next(line.split(b":", 1)[1] for line in headers.split(b"\r\n")
                                      if line.startswith(b"Content-Length:")))
                    assert 0 < length < 512 and len(body) <= length
                    if len(body) == length:
                        assert json.loads(body)["error"]["type"] == "overloaded_error"
                    # A reset may interrupt delivery even after complete headers.
            assert bridge.secret.encode() not in raw and calls == ["idle-stream"]
            assert len(bridge.server.connections) == 1 and len(bridge.server.processes) == 1
        finally:
            overloaded.close()
            sender.join(timeout=3)
            assert not sender.is_alive()
            response.close()
            first.close()


def test_shutdown_wakes_pending_model_requests_without_waiting_for_admission_deadline(peer):
    command, _, _, calls = peer
    bridge = relay.Bridge(command("idle-stream"), max_requests=1, max_pending=1,
                          admission_timeout=30).start()
    first, response = post(bridge)
    assert response.read(len(b"data: first\n\n")) == b"data: first\n\n"
    pending = socket.create_connection(("127.0.0.1", bridge.port), timeout=3)
    try:
        pending.sendall((f"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1:{bridge.port}\r\n"
                         f"Authorization: Bearer {bridge.secret}\r\nContent-Type: application/json\r\n"
                         "Content-Length: 2\r\n\r\n{}").encode())
        wait_until(lambda: len(bridge.server.connections) == 2)
        began = time.monotonic()
        bridge.close()
        wait_until(lambda: not bridge.server.connections and not bridge.server.processes)
        assert time.monotonic() - began < 3 and calls == ["idle-stream"]
    finally:
        response.close()
        first.close()
        pending.close()
        bridge.close()


@pytest.mark.parametrize("value", [
    {"version": 1, "status": 200, "headers": {}},
    {"version": True, "status": 200, "headers": {}},
    {"version": 2, "status": True, "headers": {}},
    {"version": 2, "status": 200, "headers": {"request-id": "bad\r\nvalue"}},
    {"version": 2, "status": 200, "headers": {}, "extra": "identity"},
])
def test_response_metadata_is_exact_and_strict(value):
    raw = json.dumps(value).encode()
    with pytest.raises(relay.RelayError):
        relay.metadata(io.BytesIO(struct.pack("!I", len(raw)) + raw))


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "25",
                                 10 ** 400, 1e300])
def test_timeouts_and_concurrency_bounds_must_be_finite_and_typed(peer, value):
    command = peer[0]()
    with pytest.raises(ValueError):
        relay.Bridge(command, request_timeout=value)
    with pytest.raises(ValueError):
        relay.Bridge(command, body_timeout=value)
    with pytest.raises(ValueError):
        relay.Bridge(command, header_timeout=value)
    with pytest.raises(ValueError):
        relay.Bridge(command, admission_timeout=value)
    with pytest.raises(ValueError):
        relay.check_status(command, timeout=value)
    assert peer[3] == []


@pytest.mark.parametrize("value", [-1, 33, True, None, "8", 1.0, float("inf")])
def test_pending_connections_are_finitely_bounded_before_listening(peer, value):
    with pytest.raises(ValueError):
        relay.Bridge(peer[0](), max_pending=value)
    assert peer[3] == []


@pytest.mark.parametrize("field", ["header_timeout", "admission_timeout"])
def test_header_and_admission_deadlines_have_a_hard_upper_bound(peer, field):
    with pytest.raises(ValueError):
        relay.Bridge(peer[0](), **{field: 31})
    assert peer[3] == []
