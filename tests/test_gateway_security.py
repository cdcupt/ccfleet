"""Adversarial wire and public HTTP boundary tests for the Messages gateway."""

import base64
import grp
import io
import json
import os
import pwd
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleetd.gateway_transport import GatewayTransportError, SSHAttempt, response_metadata
from tests.test_gateway_end_to_end import public_key, raw_request, request, wait_for
from tests.test_gateway_end_to_end import scenario as scenario


def framed(raw):
    if isinstance(raw, dict):
        raw = json.dumps(raw, separators=(",", ":")).encode()
    return struct.pack("!I", len(raw)) + raw


def reply(**changes):
    return {"version": 2, "status": 200,
            "headers": {"content-type": "text/event-stream"}, "retryable": False, **changes}


@pytest.mark.parametrize("raw", [
    reply(version=True), reply(version=1), reply(status=True), reply(status=101),
    reply(status=302), reply(status=600), reply(retryable=1), reply(retryable=True),
    reply(headers={"set-cookie": "PRIVATE_COOKIE"}),
    reply(headers={"content-type": "text/html"}),
    reply(headers={"content-type": ["application/json"]}),
    reply(headers={"retry-after": "-1"}), reply(headers={"retry-after": "86401"}),
    reply(headers={"retry-after": "1\r\nAuthorization: PRIVATE_TOKEN"}),
    reply(extra="PRIVATE_DETAIL"),
    b'{"version":2,"status":200,"status":429,"headers":{},"retryable":false}',
    b'{"version":2,"status":NaN,"headers":{},"retryable":false}',
])
def test_slot_cannot_forge_response_schema_headers_or_retry_permission(raw):
    with pytest.raises(GatewayTransportError) as caught:
        response_metadata(io.BytesIO(framed(raw)))
    assert caught.value.retryable is False
    assert "PRIVATE_" not in str(caught.value)


@pytest.mark.parametrize("raw", [b"", b"\0\0\0", struct.pack("!I", 0),
                                  struct.pack("!I", 32769),
                                  struct.pack("!I", 100) + b"{}"])
def test_truncated_or_oversized_slot_metadata_never_authorizes_retry(raw):
    with pytest.raises(GatewayTransportError) as caught:
        response_metadata(io.BytesIO(raw))
    assert caught.value.retryable is False


PEER = r'''
import json, pathlib, struct, sys
capture, mode = pathlib.Path(sys.argv[1]), sys.argv[2]
def read_exact(n):
    data = b""
    while len(data) < n:
        part = sys.stdin.buffer.read(n-len(data))
        if not part:
            raise EOFError()
        data += part
    return data
def metadata():
    return json.loads(read_exact(struct.unpack("!I", read_exact(4))[0]))
def frame(data):
    sys.stdout.buffer.write(struct.pack("!I", len(data)) + data)
    sys.stdout.buffer.flush()
first = metadata()
capture.write_text(json.dumps({"first": first}))
status = 401 if mode == "not-ready" else 200
frame(json.dumps({"version":2,"status":status,
                  "headers":{"content-type":"application/json"},
                  "retryable":False}).encode())
frame(b'{"ready":true,"protocol":2}' if mode != "forged-ready" else b'{"ready":false,"protocol":2}')
frame(b"")
if mode not in ("ready", "ambiguous"):
    try:
        second = metadata()
        capture.write_text(json.dumps({"first":first,"second":second}))
    except EOFError:
        pass
    sys.exit(2)
second = metadata()
body = read_exact(second["body_size"])
capture.write_text(json.dumps({"first":first,"second":second,"body":json.loads(body)}))
if mode == "ready":
    frame(json.dumps({"version":2,"status":200,
                      "headers":{"content-type":"application/json"},
                      "retryable":False}).encode())
    frame(b'{"type":"message","content":[]}')
    frame(b"")
'''


def local_peer(tmp_path, mode):
    script = tmp_path / "peer.py"
    script.write_text(PEER)
    capture = tmp_path / "capture.json"
    attempt = SSHAttempt({}, SimpleNamespace())
    attempt._command = lambda: [sys.executable, "-u", str(script), str(capture), mode]
    return attempt, capture


def test_actual_subprocess_handshake_sends_status_before_any_model_request(tmp_path):
    attempt, capture = local_peer(tmp_path, "ready")
    body = b'{"model":"opus","messages":[{"role":"user","content":"question"}]}'
    try:
        attempt.start({"content-type": "application/json"}, body,
                      path="/v1/messages/count_tokens")
        assert attempt.metadata() == reply(headers={"content-type": "application/json"})
        assert b"".join(attempt.chunks()) == b'{"type":"message","content":[]}'
        attempt.finish()
        captured = json.loads(capture.read_text())
        assert captured["first"] == {"version": 2, "operation": "status"}
        assert captured["second"]["operation"] == "request"
        assert captured["second"]["path"] == "/v1/messages/count_tokens"
        assert captured["body"] == json.loads(body)
        assert attempt.model_started is True
    finally:
        attempt.close()


@pytest.mark.parametrize("mode", ["not-ready", "forged-ready"])
def test_unready_or_fabricated_readiness_sends_no_model_frame(tmp_path, mode):
    attempt, capture = local_peer(tmp_path, mode)
    try:
        with pytest.raises(GatewayTransportError) as caught:
            attempt.start({"content-type": "application/json"}, b'{"PRIVATE_PROMPT":"question"}')
        assert caught.value.retryable is True
        assert attempt.model_started is False
        assert json.loads(capture.read_text()) == {"first": {"version": 2, "operation": "status"}}
    finally:
        attempt.close()


def test_metadata_failure_after_model_frame_is_ambiguous_and_not_retryable(tmp_path):
    attempt, capture = local_peer(tmp_path, "ambiguous")
    try:
        attempt.start({"content-type": "application/json"}, b'{"model":"opus","messages":[]}')
        assert attempt.model_started is True
        with pytest.raises(GatewayTransportError) as caught:
            attempt.metadata()
        assert caught.value.retryable is False
        assert json.loads(capture.read_text())["second"]["operation"] == "request"
    finally:
        attempt.close()


def test_authorization_is_rechecked_after_readiness_before_writing_the_model_frame(tmp_path):
    attempt, capture = local_peer(tmp_path, "ready")
    try:
        with pytest.raises(GatewayTransportError) as caught:
            attempt.start({"content-type": "application/json"}, b'{"PRIVATE_PROMPT":"question"}',
                          before_model_send=lambda: False)
        assert caught.value.status == 403
        assert caught.value.retryable is False
        assert attempt.model_started is False
        assert json.loads(capture.read_text()) == {"first": {"version": 2, "operation": "status"}}
    finally:
        attempt.close()


@pytest.mark.parametrize("mode,expected", [("ready", True), ("not-ready", False),
                                          ("forged-ready", False)])
def test_readiness_probe_uses_the_real_handshake_without_sending_a_model_frame(tmp_path, mode, expected):
    attempt, capture = local_peer(tmp_path, mode)
    assert attempt.check() is expected
    assert attempt.model_started is False
    assert attempt.closed.is_set()
    assert attempt.process.poll() is not None
    assert attempt.process.stdin.closed and attempt.process.stdout.closed
    assert json.loads(capture.read_text()) == {"first": {"version": 2, "operation": "status"}}


def test_cancellation_during_command_creation_cannot_launch_an_untracked_child(tmp_path):
    attempt, capture = local_peer(tmp_path, "ready")
    command = attempt._command
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    outcomes = []

    def slow_command():
        entered.set()
        assert release.wait(3)
        return command()

    def start():
        try:
            attempt.start({"content-type": "application/json"}, b'{"PRIVATE_PROMPT":"question"}')
        except GatewayTransportError as exc:
            outcomes.append(exc)
        finally:
            finished.set()

    attempt._command = slow_command
    worker = threading.Thread(target=start, daemon=True)
    worker.start()
    assert entered.wait(3)
    attempt.close()
    release.set()
    assert finished.wait(3)
    worker.join(timeout=1)
    try:
        assert outcomes
        assert attempt.model_started is False
        assert not capture.exists()
        assert attempt.process is None or attempt.process.poll() is not None
    finally:
        attempt.close()


def test_cancellation_during_popen_publication_kills_and_reaps_the_created_child(tmp_path, monkeypatch):
    from ccfleetd import gateway_transport

    attempt, capture = local_peer(tmp_path, "ready")
    created, release, finished = threading.Event(), threading.Event(), threading.Event()
    native_popen = gateway_transport.subprocess.Popen
    children, outcomes = [], []

    def delayed_popen(*args, **kwargs):
        child = native_popen(*args, **kwargs)
        children.append(child)
        created.set()
        assert release.wait(3)
        return child

    def start():
        try:
            attempt.start({"content-type": "application/json"}, b'{"PRIVATE_PROMPT":"question"}')
        except GatewayTransportError as exc:
            outcomes.append(exc)
        finally:
            finished.set()

    monkeypatch.setattr(gateway_transport.subprocess, "Popen", delayed_popen)
    worker = threading.Thread(target=start, daemon=True)
    worker.start()
    assert created.wait(3)
    attempt.close()
    release.set()
    assert finished.wait(3)
    worker.join(timeout=1)
    try:
        assert outcomes and not attempt.model_started
        assert not capture.exists()
        assert children[0].poll() is not None
        assert children[0].stdin.closed and children[0].stdout.closed
    finally:
        attempt.close()


def service_attempt(tmp_path, **changes):
    key = tmp_path / "service-key"
    key.write_text("SYNTHETIC_SERVICE_PRIVATE_KEY")
    key.chmod(0o600)
    kind = b"ssh-ed25519"
    blob = struct.pack("!I", len(kind)) + kind + struct.pack("!I", 32) + b"a" * 32
    target = {"unix_user": "slot01", "access_host": "192.0.2.1", "access_port": 2222,
              "ssh_host_key": "ssh-ed25519 " + base64.b64encode(blob).decode(), **changes}
    return SSHAttempt(target, SimpleNamespace(gateway_ssh_key_path=str(key))), key


def test_server_slot_command_pins_host_and_removes_shell_and_forwarding_capabilities(tmp_path):
    attempt, key = service_attempt(tmp_path)
    try:
        command = attempt._command()
        assert command[-2:] == ["slot01@192.0.2.1", "ccfleet-gateway-v1"]
        for option in ("StrictHostKeyChecking=yes", "IdentitiesOnly=yes", "BatchMode=yes",
                       "ClearAllForwardings=yes", "ForwardAgent=no", "PermitLocalCommand=no",
                       "PasswordAuthentication=no", "KbdInteractiveAuthentication=no"):
            assert option in command
        assert "-T" in command and command[command.index("-F") + 1] == "/dev/null"
        assert "IdentityFile=" + str(key) in command
        assert "SYNTHETIC_SERVICE_PRIVATE_KEY" not in " ".join(command)
        known = next(value.split("=", 1)[1] for value in command if value.startswith("UserKnownHostsFile="))
        assert os.stat(known).st_mode & 0o077 == 0
    finally:
        attempt.close()


@pytest.mark.parametrize("changes", [
    {"unix_user": "slot01;id"}, {"unix_user": "root@host"},
    {"access_host": "-oProxyCommand=sh"}, {"access_host": "host $(id)"},
    {"access_port": True}, {"access_port": 0}, {"access_port": 65536},
    {"ssh_host_key": 'command="sh" ssh-ed25519 BAD'},
])
def test_malformed_connection_descriptor_cannot_change_ssh_command(tmp_path, changes):
    attempt, _ = service_attempt(tmp_path, **changes)
    try:
        with pytest.raises(GatewayTransportError):
            attempt._command()
    finally:
        attempt.close()


@pytest.mark.parametrize("kind", ["public-mode", "symlink", "hardlink", "empty"])
def test_service_private_key_cannot_be_public_linked_or_empty(tmp_path, kind):
    attempt, key = service_attempt(tmp_path)
    if kind == "public-mode":
        key.chmod(0o644)
    elif kind == "symlink":
        destination = tmp_path / "destination"
        key.rename(destination)
        key.symlink_to(destination)
    elif kind == "hardlink":
        os.link(key, tmp_path / "linked")
    else:
        key.write_text("")
    try:
        with pytest.raises(GatewayTransportError) as caught:
            attempt._command()
        assert caught.value.status == 503
        assert caught.value.retryable is False
        assert "SYNTHETIC_" not in str(caught.value)
    finally:
        attempt.close()


@pytest.mark.parametrize("fields", [
    "Content-Length: 2\r\nContent-Length: 2\r\nContent-Type: application/json\r\n",
    "Content-Length: 2\r\nContent-Length: 3\r\nContent-Type: application/json\r\n",
    "Content-Length: 02\r\nContent-Type: application/json\r\n",
    "Content-Length: +2\r\nContent-Type: application/json\r\n",
    "Content-Length: 2\r\nTransfer-Encoding: chunked\r\nContent-Type: application/json\r\n",
    "Content-Length: 2\r\nTransfer-Encoding: \r\nContent-Type: application/json\r\n",
    "Content-Length: 2\r\nContent-Encoding: gzip\r\nContent-Type: application/json\r\n",
    "Content-Length: 2\r\nOrigin: null\r\nContent-Type: application/json\r\n",
    "Content-Length: 2\r\nExpect: 100-continue\r\nContent-Type: application/json\r\n",
    "Content-Length: 2\r\nAuthorization: Bearer PRIVATE_TOKEN\r\nContent-Type: application/json\r\n",
    "Content-Length: 2\r\nx-api-key: PRIVATE_TOKEN\r\nContent-Type: application/json\r\n",
    "Content-Length: 2\r\nContent-Type: application/json\r\nContent-Type: text/plain\r\n",
    "Content-Length: 2\r\nContent-Type: application/json\r\nX-CCFleet-Session: session-one\r\nX-CCFleet-Session: session-two\r\n",
    "Content-Length: 2\r\nContent-Type: application/json\r\nX-Private: line-one\r\n line-two\r\n",
])
def test_public_http_ambiguity_and_browser_requests_fail_before_slot_transport(scenario, fields):
    answer = raw_request(scenario, fields)
    status = int(answer.split(b" ", 2)[1])
    assert 400 <= status < 500
    assert b"PRIVATE_" not in answer
    assert not scenario.pool.attempts


@pytest.mark.parametrize("target", [
    "https://PRIVATE_USER:PRIVATE_PASSWORD@evil.invalid/v1/messages",
    "//evil.invalid/v1/messages", "/v1/messages?beta=true&url=https://evil.invalid",
    "/v1/messages/../../api/heartbeat", "/v1/messages%3Fbeta=true",
])
def test_public_gateway_is_not_an_arbitrary_url_or_endpoint_proxy(scenario, target):
    answer = raw_request(scenario, "Content-Length: 2\r\nContent-Type: application/json\r\n", target=target)
    assert 400 <= int(answer.split(b" ", 2)[1]) < 500
    assert b"PRIVATE_" not in answer
    assert not scenario.pool.attempts


@pytest.mark.parametrize("raw", [
    b"[]", b"null", b'{"model":"opus","metadata":"PRIVATE_METADATA"}',
    b'{"model":"opus","model":"sonnet","messages":[]}',
    b'{"model":"opus","max_tokens":NaN,"messages":[]}',
    b'{"model":"opus","messages":' + b"[" * 1200 + b"0" + b"]" * 1200 + b"}",
])
def test_ambiguous_or_invalid_json_is_rejected_before_any_slot_transport(scenario, raw):
    status, body, _ = request(scenario, raw)
    assert status == 400 and json.loads(body)["type"] == "error"
    assert b"PRIVATE_" not in body
    assert not scenario.pool.attempts


@pytest.mark.parametrize("token", ["", "ccf_dev_" + "a" * 43, "ccf_pair_" + "a" * 43,
                                  "a" * 64, "ccf_api_" + "a" * 43])
def test_gateway_key_namespace_does_not_accept_device_pairing_or_operator_secrets(scenario, token):
    status, body, _ = request(scenario, token=token)
    assert status == 401 and json.loads(body)["error"]["type"] == "authentication_error"
    assert not scenario.pool.attempts


def test_header_and_model_content_secrets_never_appear_in_http_access_logs(scenario, caplog):
    body = {"model": "opus", "messages": [{"role": "user", "content": "PRIVATE_PROMPT_CONTENT"}],
            "metadata": {"user_id": "PRIVATE_USER_METADATA"}}
    with caplog.at_level("INFO", logger="ccfleetd.api"):
        assert request(scenario, body, headers={"X-CCFleet-Session": "PRIVATE_SESSION"})[0] == 200
    assert scenario.key["key_token"] not in caplog.text
    assert "PRIVATE_" not in caplog.text
    assert "status=200" in caplog.text


def test_json_expansion_limit_rejects_many_small_objects_before_transport(scenario):
    raw = b'{"model":"opus","messages":[' + b'{},' * 100005 + b'{}]}'
    status, _, _ = request(scenario, raw)
    assert status == 400
    assert not scenario.pool.attempts


def test_json_complexity_scanner_does_not_count_punctuation_inside_model_text(scenario):
    content = '{}[],:"\\' * 30000
    body = {"model": "opus", "messages": [{"role": "user", "content": content}]}
    assert request(scenario, body)[0] == 200
    assert scenario.pool.requests[0]["body"] == body


def test_oversized_public_request_is_rejected_without_waiting_for_upload(scenario):
    answer = raw_request(scenario, "Content-Type: application/json\r\nContent-Length: "
                         + str(scenario.cfg.gateway_max_body_bytes + 1) + "\r\n", body=b"")
    assert int(answer.split(b" ", 2)[1]) == 413
    assert not scenario.pool.attempts
    assert scenario.runtime.body_bytes == 0


def test_aggregate_pre_auth_header_memory_is_bounded(scenario):
    fields = "Content-Length: 2\r\nContent-Type: application/json\r\n" + "".join(
        f"X-Private-{index}: " + "PRIVATE_HEADER" * 1000 + "\r\n" for index in range(6))
    answer = raw_request(scenario, fields)
    assert int(answer.split(b" ", 2)[1]) == 431
    assert b"PRIVATE_HEADER" not in answer
    assert not scenario.pool.attempts


@pytest.mark.parametrize("scenario", [{"gateway_header_timeout_s": 1}], indirect=True)
def test_idle_partial_headers_expire_without_reaching_model_transport(scenario):
    sock = socket.create_connection(scenario.server.server_address, timeout=3)
    sock.sendall(b"POST /v1/messages HTTP/1.1\r\nX-Private: PRIVATE_UNFINISHED")
    try:
        try:
            assert sock.recv(1024) == b""
        except ConnectionResetError:
            pass
    finally:
        sock.close()
    assert not scenario.pool.attempts
    wait_for(lambda: scenario.server.admissions._value == scenario.cfg.gateway_max_connections)


@pytest.mark.parametrize("scenario", [{"gateway_max_connections": 8,
                                      "gateway_header_timeout_s": 2}], indirect=True)
def test_pre_auth_connections_are_bounded_before_handler_threads(scenario):
    peers = []
    try:
        for _ in range(8):
            peer = socket.create_connection(scenario.server.server_address, timeout=3)
            peer.sendall(b"POST /v1/messages HTTP/1.1\r\n")
            peers.append(peer)
        wait_for(lambda: scenario.server.admissions._value == 0)
        answer = raw_request(scenario, "Content-Length: 2\r\nContent-Type: application/json\r\n")
        assert int(answer.split(b" ", 2)[1]) == 503
        assert not scenario.pool.attempts
    finally:
        for peer in peers:
            peer.close()
    wait_for(lambda: scenario.server.admissions._value == 8)
    assert request(scenario)[0] == 200


def paired_device_token(scenario):
    code = scenario.store.request_cli_pairing(scenario.own[0], "owner-a", now=time.time())
    return scenario.store.register_cli_device(code, public_key(5), "Paired computer",
                                              now=time.time())["device_token"]


@pytest.mark.parametrize("fields", [
    "Content-Length: 2\r\nContent-Length: 2\r\nContent-Type: application/json\r\n",
    "Content-Length: 2\r\nTransfer-Encoding: chunked\r\nContent-Type: application/json\r\n",
    "Content-Length: 02\r\nContent-Type: application/json\r\n",
    "Content-Length: 2\r\nOrigin: null\r\nContent-Type: application/json\r\n",
    "Content-Length: 2\r\nContent-Encoding: gzip\r\nContent-Type: application/json\r\n",
])
def test_endpoint_enrollment_rejects_ambiguous_http_before_creating_a_key(scenario, fields):
    token = paired_device_token(scenario)
    before = scenario.store.list_gateway_keys("owner-a")
    answer = raw_request(scenario, fields, target="/api/cli/gateway", token=token)
    assert int(answer.split(b" ", 2)[1]) == 400
    assert scenario.store.list_gateway_keys("owner-a") == before


@pytest.mark.parametrize("path,body", [
    ("/api/cli/gateway", b'{"slot_ids":["s1"],"slot_ids":["s2"]}'),
    ("/api/cli/gateway/revoke", b'{"key_id":"first","key_id":"second"}'),
])
def test_endpoint_credential_actions_reject_duplicate_json_fields(scenario, path, body):
    token = paired_device_token(scenario)
    before = scenario.store.list_gateway_keys("owner-a")
    answer = raw_request(scenario, "Content-Type: application/json\r\nContent-Length: "
                         + str(len(body)) + "\r\n", target=path, body=body, token=token)
    assert int(answer.split(b" ", 2)[1]) == 400
    assert scenario.store.list_gateway_keys("owner-a") == before


def test_model_endpoint_key_cannot_mint_another_credential(scenario):
    answer = raw_request(scenario, "Content-Type: application/json\r\nContent-Length: 2\r\n",
                         target="/api/cli/gateway")
    assert int(answer.split(b" ", 2)[1]) == 401
    assert len(scenario.store.list_gateway_keys("owner-a")) == 1


def test_valid_paired_enrollment_returns_secret_once_and_rejects_foreign_slots(scenario):
    token = paired_device_token(scenario)
    body = json.dumps({"name": "Endpoint", "slot_ids": scenario.own,
                       "mode": "priority"}).encode()
    answer = raw_request(scenario, "Content-Type: application/json\r\nContent-Length: "
                         + str(len(body)) + "\r\n", target="/api/cli/gateway", body=body, token=token)
    assert int(answer.split(b" ", 2)[1]) == 201
    header, data = answer.split(b"\r\n\r\n", 1)
    issued = json.loads(data)
    assert issued["secret"].startswith("ccf_api_")
    assert b"Cache-Control: no-store" in header
    assert issued["key"]["slot_ids"] == scenario.own and issued["key"]["mode"] == "priority"
    assert issued["secret"] not in json.dumps(scenario.store.list_gateway_keys("owner-a"))
    foreign = json.dumps({"slot_ids": [scenario.foreign]}).encode()
    answer = raw_request(scenario, "Content-Type: application/json\r\nContent-Length: "
                         + str(len(foreign)) + "\r\n", target="/api/cli/gateway", body=foreign, token=token)
    assert int(answer.split(b" ", 2)[1]) == 400
    assert len(scenario.store.list_gateway_keys("owner-a")) == 2


@pytest.fixture
def isolated_gateway_sshd(request):
    """An opt-in root/Linux test that touches only generated account/temp state.

    Prerequisites: sshd, ssh, ssh-keygen, useradd/userdel, groupadd/groupdel,
    usermod, bash, and pytest. The daemon binds only 127.0.0.1 on a random port.
    No existing account, key file, service or sshd configuration is changed.
    """
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("isolated real sshd regression requires root on Linux")
    required = ("sshd", "ssh", "ssh-keygen", "useradd", "userdel", "groupadd", "groupdel", "usermod")
    commands = {name: shutil.which(name) for name in required}
    if (any(value is None for value in commands.values())
            or not Path("/bin/bash").is_file() or not Path("/bin/dash").is_file()):
        pytest.skip("isolated real sshd regression dependencies are unavailable")
    if not Path("/run/sshd").is_dir():
        pytest.skip("sshd privilege-separation directory /run/sshd is absent")
    name = "ccfgw" + uuid.uuid4().hex[:12]
    group = name
    root = Path(tempfile.mkdtemp(prefix="ccfleet-gateway-sshd-", dir="/var/lib"))
    root.chmod(0o755)
    user_created = group_created = False
    daemon = log = None

    def command(args, **kwargs):
        return subprocess.run(args, capture_output=True, timeout=10, check=True, **kwargs)

    try:
        command([commands["groupadd"], group])
        group_created = True
        home = root / "home"
        home.mkdir(mode=0o700)
        shell = getattr(request, "param", "/bin/dash")
        command([commands["useradd"], "--no-create-home", "--home-dir", str(home),
                 "--shell", shell, "--gid", group, name])
        user_created = True
        command([commands["usermod"], "--password", "x", name])
        account = pwd.getpwnam(name)
        os.chown(home, account.pw_uid, account.pw_gid)
        private = root / "private"
        private.mkdir(mode=0o700)
        keys = root / "authorized_keys"
        keys.mkdir(mode=0o755)
        library = root / "lib"
        library.mkdir(mode=0o755)
        entry = library / "slot-entry.sh"
        source = Path(__file__).resolve().parents[1] / "node/slot-entry.sh"
        shutil.copyfile(source, entry)
        entry.chmod(0o755)
        agent = library / "ccfleet_agent"
        agent.mkdir(mode=0o755)
        (agent / "local_relay.py").write_text(
            "import sys\nprint('RELAY:' + ' '.join(sys.argv[1:]))\n")
        (agent / "project_access.py").write_text("print('PROJECT_ACCESS')\n")
        (agent / "local_relay.py").chmod(0o644)
        (agent / "project_access.py").chmod(0o644)
        for key_name in ("host", "device", "service", "home-only"):
            command([commands["ssh-keygen"], "-q", "-t", "ed25519", "-N", "", "-f", str(private / key_name)])
        service = " ".join((private / "service.pub").read_text().split()[:2])
        device = " ".join((private / "device.pub").read_text().split()[:2])
        options = "no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-user-rc"
        authorized = keys / name
        authorized.write_text(f'command="{entry}",{options} {device}\n'
                              f'command="{entry} --gateway-only",no-pty,{options} {service}\n')
        authorized.chmod(0o644)
        ssh_home = home / ".ssh"
        ssh_home.mkdir(mode=0o700)
        os.chown(ssh_home, account.pw_uid, account.pw_gid)
        home_key = ssh_home / "authorized_keys"
        home_key.write_text((private / "home-only.pub").read_text())
        home_key.chmod(0o600)
        os.chown(home_key, account.pw_uid, account.pw_gid)
        known = private / "known_hosts"
        known.write_text("ccfleet-isolated-gateway " + " ".join((private / "host.pub").read_text().split()[:2]) + "\n")
        known.chmod(0o600)
        reservation = socket.socket()
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
        reservation.close()
        config = private / "sshd_config"
        config.write_text(f"""Port {port}
ListenAddress 127.0.0.1
HostKey {private / 'host'}
PidFile {private / 'sshd.pid'}
AllowUsers {name}
UsePAM no
StrictModes yes
PubkeyAuthentication yes
AuthenticationMethods publickey
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PermitUserEnvironment no
LogLevel ERROR
Match Group {group}
    AuthorizedKeysFile {keys}/%u
    AuthorizedKeysCommand none
    TrustedUserCAKeys none
    AllowAgentForwarding no
    AllowTcpForwarding no
    AllowStreamLocalForwarding no
    X11Forwarding no
    PermitTunnel no
    PermitUserRC no
Match all
""")
        command([commands["sshd"], "-t", "-f", str(config)])
        log = (private / "sshd.log").open("wb")
        daemon = subprocess.Popen([commands["sshd"], "-D", "-e", "-f", str(config)],
                                  stdin=subprocess.DEVNULL, stdout=log, stderr=log)

        def listening():
            if daemon.poll() is not None:
                pytest.fail("isolated synthetic sshd exited before listening")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return True
            except OSError:
                return False

        wait_for(listening)

        def base(role):
            return [commands["ssh"], "-T", "-F", "/dev/null", "-p", str(port),
                    "-o", "HostKeyAlias=ccfleet-isolated-gateway",
                    "-o", f"UserKnownHostsFile={known}", "-o", "GlobalKnownHostsFile=/dev/null",
                    "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes",
                    "-o", f"IdentityFile={private / role}", "-o", "BatchMode=yes",
                    "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
                    "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no",
                    "-o", "ConnectTimeout=3"]

        def connect(role, original, flags=(), environment=None):
            return subprocess.run([*base(role), *flags, f"{name}@127.0.0.1", original],
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=8,
                                  env={**os.environ, **environment} if environment else None)

        yield SimpleNamespace(name=name, home=home, root=root, config=config, account=account,
                              connect=connect, base=base, port=port, commands=commands)
    finally:
        if daemon is not None:
            daemon.terminate()
            try:
                daemon.wait(timeout=3)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait(timeout=3)
        if log is not None:
            log.close()
        try:
            if user_created:
                subprocess.run([commands["userdel"], name], capture_output=True, timeout=10, check=True)
        finally:
            try:
                if group_created:
                    try:
                        grp.getgrnam(group)
                    except KeyError:
                        pass
                    else:
                        subprocess.run([commands["groupdel"], group], capture_output=True, timeout=10, check=True)
            finally:
                shutil.rmtree(root)


def test_real_sshd_keeps_gateway_and_device_forced_commands_separate(isolated_gateway_sshd):
    sshd = isolated_gateway_sshd
    effective = subprocess.run([sshd.commands["sshd"], "-T", "-f", str(sshd.config),
                                "-C", f"user={sshd.name},host=localhost,addr=127.0.0.1"],
                               capture_output=True, timeout=5, check=True).stdout.decode()
    for value in ("forcecommand none", "authorizedkeyscommand none", "trustedusercakeys none",
                  "authenticationmethods publickey", "permituserrc no", "allowtcpforwarding no",
                  "allowstreamlocalforwarding no", "permituserenvironment no"):
        assert value in effective
    service = sshd.connect("service", "ccfleet-gateway-v1")
    assert service.returncode == 0 and service.stdout == b"RELAY:--gateway-protocol-v1\n"
    device = sshd.connect("device", "ccfleet-inference-v1")
    assert device.returncode == 0 and device.stdout == b"RELAY:--protocol-v2\n"
    for original in ("ccfleet-inference-v1", "ccfleet-project-v1", "sh -c id",
                     "ccfleet-gateway-v1 --shell"):
        rejected = sshd.connect("service", original)
        assert rejected.returncode == 2 and b"unsupported CC Fleet gateway request" in rejected.stderr


def test_real_sshd_rejects_home_keys_pty_and_forwarding(isolated_gateway_sshd):
    sshd = isolated_gateway_sshd
    home = sshd.connect("home-only", "ccfleet-gateway-v1")
    assert home.returncode == 255 and b"Permission denied" in home.stderr
    pty = sshd.connect("service", "ccfleet-gateway-v1", flags=("-tt",))
    assert b"PTY allocation request failed" in pty.stderr
    forward = subprocess.run([*sshd.base("service"), "-W", f"127.0.0.1:{sshd.port}",
                              f"{sshd.name}@127.0.0.1"], stdin=subprocess.DEVNULL,
                             capture_output=True, timeout=8)
    assert forward.returncode == 255
    assert b"administratively prohibited" in forward.stderr


def test_real_sshd_slot_startup_files_cannot_bypass_gateway_dispatcher(isolated_gateway_sshd):
    sshd = isolated_gateway_sshd
    startup = sshd.home / ".bashrc"
    startup.write_text("printf 'USER_STARTUP_BYPASS\\n'\nexit 0\n")
    os.chown(startup, sshd.account.pw_uid, sshd.account.pw_gid)
    profile = sshd.home / ".profile"
    profile.write_text("printf 'USER_PROFILE_BYPASS\\n'\nexit 0\n")
    os.chown(profile, sshd.account.pw_uid, sshd.account.pw_gid)
    ssh_rc = sshd.home / ".ssh/rc"
    ssh_rc.write_text("printf 'USER_SSH_RC_BYPASS\\n'\n")
    os.chown(ssh_rc, sshd.account.pw_uid, sshd.account.pw_gid)
    env = sshd.home / ".ssh/environment"
    env.write_text("BASH_ENV=" + str(startup) + "\nENV=" + str(profile) + "\n")
    os.chown(env, sshd.account.pw_uid, sshd.account.pw_gid)
    reply = sshd.connect("service", "ccfleet-gateway-v1", flags=("-o", "SendEnv=BASH_ENV ENV"),
                         environment={"BASH_ENV": str(startup), "ENV": str(profile)})
    assert reply.returncode == 0 and reply.stdout == b"RELAY:--gateway-protocol-v1\n"


@pytest.mark.parametrize("isolated_gateway_sshd", ["/bin/bash"], indirect=True)
def test_real_sshd_bash_startup_reproduces_the_previous_forced_command_bypass(isolated_gateway_sshd):
    """Document why a holder-writable Bash startup environment is unsafe here."""
    sshd = isolated_gateway_sshd
    startup = sshd.home / ".bashrc"
    startup.write_text("printf 'USER_STARTUP_BYPASS\\n'\nexit 0\n")
    os.chown(startup, sshd.account.pw_uid, sshd.account.pw_gid)
    reply = sshd.connect("service", "ccfleet-gateway-v1")
    assert reply.returncode == 0 and reply.stdout == b"USER_STARTUP_BYPASS\n"
