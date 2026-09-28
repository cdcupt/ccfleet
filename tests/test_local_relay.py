"""Local execution, slot-account isolation, and the new streaming boundary."""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import runpy
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleet_agent import local_relay as relay

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def slot(tmp_path):
    home = tmp_path / "slot"
    (home / ".claude").mkdir(parents=True)
    (home / ".config/ccfleet").mkdir(parents=True)
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "owner-a"}}))
    fp = hashlib.sha256(b"owner-a").hexdigest()[:16]
    (home / ".config/ccfleet/slot-state.json").write_text(json.dumps({"bound_fp": fp}))
    (home / ".claude/.credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "slot-only-secret", "refreshToken": "never-read-or-forwarded",
        "expiresAt": (time.time() + 3600) * 1000}}))
    return home


def request(body=b'{"messages":[],"metadata":{"user_id":"local-identity"}}', **kwargs):
    meta = {"version": 1, "method": "POST", "path": "/v1/messages",
            "headers": {"content-type": "application/json", "anthropic-beta": "real-cli-beta",
                        "user-agent": "real-local-cli", "x-stainless-os": "MacOS",
                        "authorization": "Bearer client-secret", "x-api-key": "other-key",
                        "cookie": "cookie-secret", "host": "evil.example"},
            "body_size": len(body), **kwargs}
    encoded = json.dumps(meta).encode()
    return struct.pack("!I", len(encoded)) + encoded + body


def response(raw):
    stream = io.BytesIO(raw)
    meta = relay.read_metadata(stream)
    chunks = []
    while True:
        size = struct.unpack("!I", relay.read_exact(stream, 4))[0]
        if not size:
            break
        chunks.append(relay.read_exact(stream, size))
    assert stream.read() == b""
    return meta, b"".join(chunks)


class Upstream:
    def __init__(self, chunks=None, fail=False):
        self.chunks = iter(chunks or [b"event: message_start\n\n", b"event: message_stop\n\n"])
        self.status = 200
        self.calls = []
        self.closed = False
        self.fail = fail
        self.sock = None

    def connect(self):
        pass

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))

    def getresponse(self):
        return self

    def getheaders(self):
        return [("Content-Type", "text/event-stream"), ("Request-ID", "test-request"),
                ("Set-Cookie", "must-not-return"), ("Location", "https://evil.example")]

    def read1(self, size):
        assert size <= relay.CHUNK_SIZE
        try:
            return next(self.chunks)
        except StopIteration:
            if self.fail:
                raise ConnectionResetError("internal secret detail") from None
            return b""

    def close(self):
        self.closed = True


def run(slot, raw=None, *, upstream=None, policy=lambda: None):
    sink = io.BytesIO()
    upstream = upstream or Upstream()
    code = relay.serve_one(io.BytesIO(raw if raw is not None else request()), sink, slot,
                          policy=policy, connect=lambda: upstream)
    return code, sink.getvalue(), upstream


def test_body_and_client_identity_are_preserved_but_only_the_slots_token_is_used(slot):
    body = b'{"messages":[],"metadata":{"user_id":"local-identity"}}'
    code, data, upstream = run(slot, request(body))
    assert code == 0 and upstream.closed
    (args, kwargs), = upstream.calls
    assert args == ("POST", "/v1/messages")
    assert kwargs["body"] == body
    headers = kwargs["headers"]
    assert headers["authorization"] == "Bearer slot-only-secret"
    assert headers["user-agent"] == "real-local-cli"
    assert headers["x-stainless-os"] == "MacOS"
    assert headers["anthropic-beta"] == "real-cli-beta"
    assert not ({"cookie", "host", "x-api-key"} & headers.keys())
    meta, content = response(data)
    assert meta["headers"] == {"content-type": "text/event-stream", "request-id": "test-request"}
    assert content == b"event: message_start\n\nevent: message_stop\n\n"
    for secret in (b"slot-only-secret", b"never-read-or-forwarded", b"client-secret"):
        assert secret not in data


def test_default_operator_policy_blocks_the_relay_before_reading_credentials(tmp_path):
    def disabled():
        relay.require_enabled(tmp_path / "missing-policy")

    code, data, upstream = run(tmp_path / "no-user-files", policy=disabled)
    assert code == 2 and response(data)[0]["status"] == 403
    assert upstream.calls == []


@pytest.mark.parametrize("parent_mode,parent_uid,marker_mode,marker_uid,allowed", [
    (stat.S_IFDIR | 0o755, 0, stat.S_IFREG | 0o644, 0, True),
    (stat.S_IFDIR | 0o755, 501, stat.S_IFREG | 0o644, 0, False),
    (stat.S_IFDIR | 0o777, 0, stat.S_IFREG | 0o644, 0, False),
    (stat.S_IFLNK | 0o755, 0, stat.S_IFREG | 0o644, 0, False),
    (stat.S_IFDIR | 0o755, 0, stat.S_IFREG | 0o644, 501, False),
    (stat.S_IFDIR | 0o755, 0, stat.S_IFREG | 0o666, 0, False),
    (stat.S_IFDIR | 0o755, 0, stat.S_IFLNK | 0o644, 0, False),
])
def test_only_root_can_enable_a_slot_and_policy_symlinks_are_not_followed(parent_mode, parent_uid, marker_mode, marker_uid, allowed):
    class PolicyPath:
        def lstat(self):
            return SimpleNamespace(st_mode=parent_mode, st_uid=parent_uid)

        def __truediv__(self, _name):
            return SimpleNamespace(lstat=lambda: SimpleNamespace(st_mode=marker_mode, st_uid=marker_uid))

    if allowed:
        relay.require_enabled(PolicyPath())
    else:
        with pytest.raises(relay.RelayError, match="operator"):
            relay.require_enabled(PolicyPath())


def test_status_probe_contains_no_account_or_secret(slot):
    code, data, upstream = run(slot, request(operation="status"))
    meta, body = response(data)
    assert code == 0 and meta["status"] == 200
    assert json.loads(body) == {"ready": True, "protocol": 1}
    assert upstream.calls == []
    assert b"owner-a" not in data and b"slot-only-secret" not in data


@pytest.mark.parametrize("changes", [
    {"path": "https://evil.example/v1/messages"}, {"path": "/v1/messages/../oauth/token"},
    {"path": "/v1/messages?host=evil.example"}, {"method": "CONNECT"},
    {"method": "GET"}, {"body_size": True}, {"body_size": relay.MAX_BODY + 1},
    {"headers": {"content-type": "application/json\r\nX-Foo: bad"}},
    {"headers": {"content-type": "text/plain"}}, {"headers": ["not", "a", "mapping"]},
    {"version": 2}, {"version": True}, {"path": []}, {"path": {}},
])
def test_invalid_requests_never_reach_upstream(slot, changes):
    code, data, upstream = run(slot, request(**changes))
    assert code == 2 and response(data)[0]["status"] in (400, 413)
    assert upstream.calls == []


@pytest.mark.parametrize("body", [b"[1,2]", b"null", b"not-json", b"\xff"])
def test_invalid_json_never_reaches_upstream(slot, body):
    code, data, upstream = run(slot, request(body))
    assert code == 2 and response(data)[0]["status"] == 400
    assert upstream.calls == []


def test_another_claude_account_cannot_use_the_bound_slot(slot):
    (slot / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "owner-b"}}))
    code, data, upstream = run(slot)
    assert code == 2 and response(data)[0]["status"] == 409
    assert upstream.calls == []


def test_account_switch_during_credential_read_cannot_relabel_an_old_token(slot, monkeypatch):
    original = relay.credential

    def switching(home, now):
        token = original(home, now)
        (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "owner-b"}}))
        fp = hashlib.sha256(b"owner-b").hexdigest()[:16]
        (home / ".config/ccfleet/slot-state.json").write_text(json.dumps({"bound_fp": fp}))
        return token

    monkeypatch.setattr(relay, "credential", switching)
    code, data, upstream = run(slot)
    assert code == 2 and response(data)[0]["status"] == 409
    assert upstream.calls == []


@pytest.mark.parametrize("expiry", [0, True, "future", float("nan"), float("inf")])
def test_expired_or_invalid_credentials_fail_without_refresh_or_file_writes(slot, expiry):
    path = slot / ".claude/.credentials.json"
    saved = json.dumps({"claudeAiOauth": {"accessToken": "secret", "expiresAt": expiry,
                                         "refreshToken": "do-not-use"}})
    path.write_text(saved)
    code, data, upstream = run(slot)
    assert code == 2 and response(data)[0]["status"] == 401
    assert upstream.calls == [] and path.read_text() == saved


def test_upstream_truncation_is_not_success_and_never_leaks_exception_text(slot):
    code, data, upstream = run(slot, upstream=Upstream(fail=True))
    assert code == 2 and upstream.closed
    assert b"internal secret detail" not in data
    with pytest.raises(EOFError):
        response(data)


def test_http_eof_with_unread_content_length_is_not_success(slot):
    upstream = Upstream()
    upstream.length = 100
    code, data, _ = run(slot, upstream=upstream)
    assert code == 2
    with pytest.raises(EOFError):
        response(data)


@pytest.mark.parametrize("reason", ["policy", "disconnect", "account"])
def test_inflight_relay_stops_on_revocation_disconnect_or_account_change(slot, reason):
    peer, stdin = socket.socketpair()
    upstream_socket, upstream_peer = socket.socketpair()
    entered = threading.Event()
    enabled = threading.Event()
    enabled.set()
    sink = io.BytesIO()

    class WaitingUpstream(Upstream):
        def __init__(self):
            super().__init__()
            self.sock = upstream_socket

        def read1(self, size):
            entered.set()
            return self.sock.recv(size)

    def policy():
        if not enabled.is_set():
            raise relay.RelayError(403, "disabled")

    source = stdin.makefile("rb", buffering=0)
    codes = []
    worker = threading.Thread(target=lambda: codes.append(relay.serve_one(
        source, sink, slot, policy=policy, connect=WaitingUpstream, watch=True)), daemon=True)
    try:
        peer.sendall(request())
        worker.start()
        assert entered.wait(5), "upstream never entered its streaming read"
        if reason == "policy":
            enabled.clear()
        elif reason == "disconnect":
            peer.shutdown(socket.SHUT_WR)
        else:
            (slot / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "new-account"}}))
        worker.join(timeout=5)
        assert not worker.is_alive() and codes == [2]
        with pytest.raises(EOFError):
            response(sink.getvalue())
    finally:
        enabled.clear()
        upstream_peer.close()
        upstream_socket.close()
        peer.close()
        stdin.close()
        source.close()
        worker.join(timeout=2)


def test_native_credential_rotation_is_observed_on_the_next_request(slot):
    old = slot / ".claude/.credentials.json"
    first = json.loads(old.read_text())
    first["claudeAiOauth"]["expiresAt"] = 1
    old.write_text(json.dumps(first))
    assert response(run(slot)[1])[0]["status"] == 401
    first["claudeAiOauth"].update(accessToken="replacement-secret", expiresAt=(time.time()+3600)*1000)
    replacement = slot / ".claude/.credentials.json.native-write"
    replacement.write_text(json.dumps(first))
    replacement.replace(old)
    code, data, upstream = run(slot)
    assert code == 0
    assert upstream.calls[0][1]["headers"]["authorization"] == "Bearer replacement-secret"
    assert b"replacement-secret" not in data


def test_upstream_address_and_certificate_validation_are_fixed(monkeypatch):
    seen = []
    monkeypatch.setattr(http.client, "HTTPSConnection", lambda *a, **kw: seen.append((a, kw)))
    relay.connect_upstream()
    args, kwargs = seen[0]
    assert args == ("api.anthropic.com", 443)
    assert kwargs["context"].check_hostname


@pytest.fixture
def local_client(monkeypatch, tmp_path):
    monkeypatch.setenv("CCFLEET_HOME", str(tmp_path / "client"))
    return runpy.run_path(str(ROOT / "laptop/ccfleet"))


def test_local_profile_is_separate_and_does_not_inherit_other_provider_credentials(local_client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "other-account-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "other-account-token")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("NO_PROXY", "internal.example,localhost")
    monkeypatch.setenv("no_proxy", "private.example")
    env = local_client["local_environment"]({"slot_id": "slot1"}, 12345, "launch-only-token")
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:12345"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "launch-only-token"
    assert "ANTHROPIC_API_KEY" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "CLAUDE_CODE_USE_BEDROCK" not in env
    assert "/local/" in env["CLAUDE_CONFIG_DIR"]
    assert env["NO_PROXY"] == "internal.example,localhost,127.0.0.1"
    assert env["no_proxy"] == "private.example,127.0.0.1,localhost"
    assert os.environ["ANTHROPIC_API_KEY"] == "other-account-key", "parent shell was modified"
    assert list(Path(env["CLAUDE_CONFIG_DIR"]).iterdir()) == []


def test_adding_lowercase_no_proxy_keeps_uppercase_only_exclusions(local_client, monkeypatch):
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("NO_PROXY", "internal.example")
    env = local_client["local_environment"]({"slot_id": "slot1"}, 12345, "nonce")
    assert env["no_proxy"] == env["NO_PROXY"] == "internal.example,127.0.0.1,localhost"


def test_laptop_permissions_do_not_silently_inherit_hosted_bypass(local_client):
    args = local_client["parser"]().parse_args(["local"])
    assert args.mode == "manual" and args.model == "opus" and args.effort == "max"
    assert args.project == "."
    args = local_client["parser"]().parse_args(["local", "--mode", "bypassPermissions"])
    assert args.mode == "bypassPermissions"


@pytest.mark.parametrize("command,accepted", [
    ("ccfleet-relay-v1", True), ("ccfleet-relay-v1 extra", False),
    ("ccfleet-relay-v1; touch unexpected", False), ("ccfleet-relay-v1\nid", False),
])
def test_forced_entry_allows_only_the_exact_nonterminal_protocol(tmp_path, command, accepted):
    entry = tmp_path / "slot-entry.sh"
    entry.write_text((ROOT / "node/slot-entry.sh").read_text())
    package = tmp_path / "ccfleet_agent"
    package.mkdir()
    (package / "local_relay.py").write_text('print("entered_fixed_relay")\n')
    proc = subprocess.run(["bash", str(entry)], env={**os.environ, "SSH_ORIGINAL_COMMAND": command},
                          input=b"", capture_output=True, timeout=10, cwd=tmp_path)
    assert (proc.returncode == 0) is accepted
    assert (b"entered_fixed_relay" in proc.stdout) is accepted
    assert not (tmp_path / "unexpected").exists()


def test_relay_ssh_does_not_enable_forwarding_or_a_general_shell(local_client, monkeypatch):
    monkeypatch.setattr(local_client["shutil"], "which", lambda _: "/usr/bin/ssh")
    command = local_client["relay_ssh_command"]({
        "device_id": "d1", "host_alias": "ccfleet-s1", "known_hosts": "/k",
        "key": "/i", "user": "slot01"})
    assert command[1] == "-T" and command[-1] == "ccfleet-relay-v1"
    assert "ClearAllForwardings=yes" in command
    assert "StrictHostKeyChecking=yes" in command
    assert "-L" not in command and "-R" not in command


@pytest.fixture
def local_http(local_client, tmp_path, monkeypatch):
    # Real local sockets and a real subprocess exercise framing and streaming;
    # no Anthropic endpoint, credentials, or production machine is contacted.
    fake = tmp_path / "fake_slot.py"
    fake.write_text('''import json, struct, sys
r, w = sys.stdin.buffer, sys.stdout.buffer
size = struct.unpack("!I", r.read(4))[0]
meta = json.loads(r.read(size))
body = r.read(meta["body_size"])
assert "authorization" not in {k.lower() for k in meta["headers"]}
reply = json.dumps({"version":1,"status":200,"headers":{"content-type":"text/event-stream"}}).encode()
w.write(struct.pack("!I",len(reply))+reply)
for part in (b"event: message_start\\n\\n", b"event: message_stop\\n\\n", b""):
    w.write(struct.pack("!I",len(part))+part)
    w.flush()
''')
    globals_ = local_client["LocalRelayHandler"].do_POST.__globals__
    monkeypatch.setitem(globals_, "relay_ssh_command", lambda _: [sys.executable, str(fake)])
    server = local_client["LocalRelayServer"]({"slot_id": "slot1"}, "local-test-secret")
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield server
    server.stop_connections()
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


def http_request(server, *, token="local-test-secret", path="/v1/messages", extra=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json", **(extra or {})}
    conn.request("POST", path, b'{"messages":[]}', headers)
    reply = conn.getresponse()
    status, body = reply.status, reply.read()
    conn.close()
    return status, body


def test_loopback_relay_streams_real_http_through_a_subprocess(local_http):
    status, body = http_request(local_http)
    assert status == 200
    assert body == b"event: message_start\n\nevent: message_stop\n\n"


@pytest.mark.parametrize("kwargs,expected", [
    ({"token": "wrong"}, 401), ({"path": "/v1/oauth/token"}, 400),
    ({"extra": {"Origin": "https://evil.example"}}, 403),
    ({"extra": {"Host": "evil.example"}}, 403),
    ({"extra": {"Transfer-Encoding": "chunked"}}, 400),
])
def test_loopback_relay_rejects_unauthorized_browser_and_nonmessage_requests(local_http, kwargs, expected):
    assert http_request(local_http, **kwargs)[0] == expected


def test_local_launcher_runs_original_claude_in_the_selected_local_directory(local_client, monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    calls = []
    globals_ = local_client["cmd_local"].__globals__
    monkeypatch.setitem(globals_, "load_config", lambda: {})
    monkeypatch.setitem(globals_, "choose_device", lambda *_: {"slot_id": "slot1", "slot_name": "test-slot"})
    monkeypatch.setitem(globals_, "check_local_relay", lambda _: None)
    monkeypatch.setattr(local_client["shutil"], "which", lambda _: "/test/original-claude")
    monkeypatch.setattr(subprocess, "call", lambda cmd, **kw: calls.append((cmd, kw)) or 0)
    args = SimpleNamespace(slot="", project=str(project), model="fable", effort="xhigh",
                           mode="manual", resume="", continue_session=False, print_prompt=None)
    assert local_client["cmd_local"](args) == 0
    command, options = calls[0]
    assert command == ["/test/original-claude", "--model", "fable", "--effort", "xhigh", "--permission-mode", "manual"]
    assert options["cwd"] == project
    assert options["env"]["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
