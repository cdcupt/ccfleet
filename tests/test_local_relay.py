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
    assert args.mode == "manual" and args.model is None and args.effort is None
    command = local_client["local_claude_command"]("claude", args)
    assert command == ["claude", "--model", "opus", "--effort", "max", "--permission-mode", "manual"]
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
    args = local_client["parser"]().parse_args([
        "local", "--project", str(project), "--model", "fable", "--effort", "xhigh"])
    assert local_client["cmd_local"](args) == 0
    command, options = calls[0]
    assert command == ["/test/original-claude", "--model", "fable", "--effort", "xhigh", "--permission-mode", "manual"]
    assert options["cwd"] == project
    assert options["env"]["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")


@pytest.mark.parametrize("flags,expected", [
    (["--resume"], ["--resume"]),
    (["--resume", "saved-session"], ["--resume=saved-session"]),
    (["--resume", "project with spaces"], ["--resume=project with spaces"]),
    (["--continue"], ["--continue"]),
    (["--continue", "--fork-session", "--name", "review"],
     ["--continue", "--fork-session", "--name=review"]),
    (["--resume", "original", "--fork-session"], ["--resume=original", "--fork-session"]),
])
def test_local_resume_preserves_native_model_and_effort_choices(local_client, flags, expected):
    args = local_client["parser"]().parse_args(["local", *flags])
    local_client["validate_local_args"](args)
    assert local_client["local_claude_command"]("claude", args) == [
        "claude", "--permission-mode", "manual", *expected]


def test_local_resume_can_explicitly_change_model_effort_and_permissions(local_client):
    args = local_client["parser"]().parse_args([
        "local", "--continue", "--model", "fable", "--effort", "low",
        "--mode", "bypassPermissions"])
    assert local_client["local_claude_command"]("claude", args) == [
        "claude", "--model", "fable", "--effort", "low",
        "--dangerously-skip-permissions", "--continue"]


def test_named_new_local_session_keeps_names_and_prompts_as_data(local_client):
    args = local_client["parser"]().parse_args([
        "local", "--new", "--name=--dangerously-skip-permissions",
        "--print=--dangerously-skip-permissions"])
    assert local_client["local_claude_command"]("claude", args) == [
        "claude", "--model", "opus", "--effort", "max", "--permission-mode", "manual",
        "--name=--dangerously-skip-permissions", "--print", "--", "--dangerously-skip-permissions"]


@pytest.mark.parametrize("flags", [
    ["--resume", "--continue"], ["--new", "--resume"], ["--new", "--continue"],
])
def test_local_history_operations_are_mutually_exclusive(local_client, flags):
    with pytest.raises(SystemExit):
        local_client["parser"]().parse_args(["local", *flags])


@pytest.mark.parametrize("flags", [
    ["--fork-session"], ["--new", "--fork-session"], ["--resume", "--print", "hi"],
    ["--name", "bad\nname"], ["--resume", "bad\x1bname"], ["--name", "a" * 257],
    ["--model", "bad;name"], ["--check", "--print", "hi"], ["--check", "--continue"],
    ["--check", "--resume"], ["--check", "--new"], ["--check", "--name", "work"],
])
def test_invalid_local_session_options_fail_before_config_or_network(local_client, monkeypatch, flags):
    def unexpected():
        pytest.fail("invalid arguments read the device configuration")
    monkeypatch.setitem(local_client["cmd_local"].__globals__, "load_config", unexpected)
    assert local_client["main"](["local", *flags]) == 2


def test_local_sessions_do_not_inherit_shell_model_or_pinned_effort(local_client, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    monkeypatch.setenv("ANTHROPIC_MODEL", "other-model")
    env = local_client["local_environment"]({"slot_id": "slot1"}, 12345, "nonce")
    assert "CLAUDE_CODE_EFFORT_LEVEL" not in env and "ANTHROPIC_MODEL" not in env
    assert os.environ["CLAUDE_CODE_EFFORT_LEVEL"] == "max"
    assert os.environ["ANTHROPIC_MODEL"] == "other-model"


@pytest.fixture
def local_launch(local_client, monkeypatch, tmp_path):
    project = tmp_path / "project with spaces"
    project.mkdir()
    calls = []
    probes = []
    globals_ = local_client["cmd_local"].__globals__
    monkeypatch.setitem(globals_, "load_config", lambda: {})
    monkeypatch.setitem(globals_, "choose_device", lambda *_: {"slot_id": "slot1", "slot_name": "test-slot"})
    monkeypatch.setitem(globals_, "check_local_relay", lambda device: probes.append(device["slot_id"]))
    monkeypatch.setattr(local_client["shutil"], "which", lambda _: "/test/original-claude")
    monkeypatch.setattr(subprocess, "call", lambda cmd, **kw: calls.append((cmd, kw)) or 0)
    return project, calls, probes


def test_local_check_sends_only_readiness_probe_and_starts_no_listener_or_claude(local_client, local_launch, monkeypatch, capsys):
    project, calls, probes = local_launch

    def unexpected(*_):
        pytest.fail("readiness check created a local listener")
    monkeypatch.setitem(local_client["cmd_local"].__globals__, "LocalRelayServer", unexpected)
    assert local_client["main"](["local", "--check", "--project", str(project)]) == 0
    assert probes == ["slot1"] and calls == []
    assert "No model request was sent" in capsys.readouterr().out
    assert not local_client["home"]().exists(), "check should not create a Claude profile"


@pytest.mark.parametrize("failure", ["missing_claude", "missing_project", "blocked_slot"])
def test_local_launch_stops_before_starting_claude_when_not_ready(local_client, local_launch, monkeypatch, failure):
    project, calls, probes = local_launch
    if failure == "missing_claude":
        monkeypatch.setattr(local_client["shutil"], "which", lambda _: None)
    elif failure == "missing_project":
        project = project / "does-not-exist"
    else:
        def blocked(_):
            raise local_client["CliError"]("slot unavailable")
        monkeypatch.setitem(local_client["cmd_local"].__globals__, "check_local_relay", blocked)
    assert local_client["main"](["local", "--project", str(project)]) == 2
    assert calls == [] and probes == []


@pytest.mark.parametrize("print_mode", [False, True])
def test_local_exit_suggests_quoted_resume_only_for_interactive_sessions(local_client, local_launch, capsys, print_mode):
    project, _, _ = local_launch
    flags = ["--print", "hello"] if print_mode else []
    assert local_client["main"](["local", "--project", str(project), *flags]) == 0
    output = capsys.readouterr().err
    if print_mode:
        assert "Resume a saved conversation" not in output
    else:
        assert f"--slot slot1 --project '{project}' --resume" in output


@pytest.mark.parametrize("status,expected", [
    (401, "sign-in needs renewal"), (403, "not enabled"),
    (409, "changing accounts"), (503, "unavailable"),
])
def test_local_preflight_errors_give_safe_specific_recovery_steps(local_client, monkeypatch, status, expected):
    encoded = io.BytesIO()
    relay.send_error(encoded, status, "remote-private-data\x1b")
    monkeypatch.setitem(local_client["check_local_relay"].__globals__, "relay_ssh_command", lambda _: ["ssh"])
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: SimpleNamespace(
        stdout=encoded.getvalue(), stderr=b"secret-debug-output", returncode=2))
    with pytest.raises(local_client["CliError"], match=expected) as error:
        local_client["check_local_relay"]({})
    assert error.value.status == status
    assert "remote-private-data" not in str(error.value) and "secret-debug-output" not in str(error.value)


def test_local_preflight_transport_failure_does_not_print_private_ssh_output(local_client, monkeypatch):
    monkeypatch.setitem(local_client["check_local_relay"].__globals__, "relay_ssh_command", lambda _: ["ssh"])
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: SimpleNamespace(
        stdout=b"", stderr=b"secret-debug-output", returncode=255))
    with pytest.raises(local_client["CliError"], match="Connected devices") as error:
        local_client["check_local_relay"]({})
    assert "secret-debug-output" not in str(error.value)
