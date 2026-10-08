"""The CLI path: one device, one held slot, and an opaque encrypted stream."""

from __future__ import annotations

import base64
import json
import os
import pty
import runpy
import socket
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleetd import cli_access, slots
from ccfleetd.desired import desired_state
from ccfleetd.store import NotYours, Store, StoreError


def public_key(byte=1):
    kind = b"ssh-ed25519"
    blob = len(kind).to_bytes(4, "big") + kind + (32).to_bytes(4, "big") + bytes([byte]) * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode()


def active_slot(store: Store):
    store.add_node("m1", "operator", now=1.0)
    store.set_node_access("m1", "192.0.2.20", 2222, public_key(9))
    store.add_account("a1", "sub-1", "a@example.com", slot_quota=1, now=1.0)
    store.add_slot("s1", "m1", "slot01", now=1.0)
    store.apply_slot_report("m1", [{"unix_user": "slot01", "present": False}], now=2.0)
    slot = store.claim_slot("a1", now=3.0)
    store.move_slot(slot["id"], slots.CLAIMED)
    store.move_slot(slot["id"], slots.ACTIVE)
    return store.get_slot("s1")


def test_pairing_code_is_hash_only_single_use_and_binds_one_key(store):
    slot = active_slot(store)
    code = store.request_cli_pairing(slot["id"], "a1", now=10.0)
    assert code.startswith(cli_access.PAIR_PREFIX)
    raw = json.dumps([dict(r) for r in store._conn.execute("SELECT * FROM cli_pairings")])
    assert code not in raw

    made = store.register_cli_device(code, public_key(), "Alice laptop", now=11.0)
    assert made["device_token"].startswith(cli_access.DEVICE_PREFIX)
    assert made["access_host"] == "192.0.2.20" and made["access_port"] == 2222
    assert store.cli_public_keys("s1") == [public_key()]
    [shown] = store.list_cli_devices("s1", held_by="a1")
    assert shown["name"] == "Alice laptop" and shown["fingerprint"].startswith("SHA256:")
    assert made["device_token"] not in json.dumps(shown)

    with pytest.raises(StoreError, match="already been used"):
        store.register_cli_device(code, public_key(2), "replay", now=12.0)


def test_device_token_resolves_only_while_the_same_account_holds_the_slot(store):
    slot = active_slot(store)
    code = store.request_cli_pairing(slot["id"], "a1", now=10.0)
    made = store.register_cli_device(code, public_key(), "laptop", now=11.0)
    found = store.resolve_cli_device(made["device_token"], now=12.0)
    assert found["slot_id"] == "s1" and found["unix_user"] == "slot01"
    assert store.cli_device_is_active(made["device_id"])
    assert store.resolve_cli_device("ccf_dev_not-a-real-token", now=12.0) is None

    store.begin_release("s1", held_by="a1")
    assert store.resolve_cli_device(made["device_token"], now=13.0) is None
    assert not store.cli_device_is_active(made["device_id"])
    assert store.cli_public_keys("s1") == []


def test_pairing_is_for_the_holder_and_a_configured_hosted_slot(store):
    slot = active_slot(store)
    store.add_account("a2", "sub-2", "b@example.com", slot_quota=1, now=1.0)
    with pytest.raises(NotYours):
        store.request_cli_pairing(slot["id"], "a2", now=10.0)
    store.clear_node_access("m1")
    with pytest.raises(StoreError, match="not ready"):
        store.request_cli_pairing(slot["id"], "a1", now=10.0)


def test_desired_state_carries_only_public_keys_to_the_slots_machine(store):
    slot = active_slot(store)
    code = store.request_cli_pairing(slot["id"], "a1", now=10.0)
    store.register_cli_device(code, public_key(), "laptop", now=11.0)
    node = store.get_node("m1")
    desired = desired_state(node, slots=[store.get_slot("s1")],
                            slot_cli_keys={"s1": store.cli_public_keys("s1")})
    assert desired["slots"][0]["ssh_public_keys"] == [public_key()]
    assert "ccf_dev_" not in json.dumps(desired)

    store.begin_release("s1", held_by="a1")
    releasing = desired_state(store.get_node("m1"), slots=[store.get_slot("s1")])
    assert releasing["slots"][0]["ssh_public_keys"] == []


def test_websocket_handshake_and_masked_binary_frames():
    assert cli_access.websocket_accept("dGhlIHNhbXBsZSBub25jZQ==") == \
        "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    left, right = socket.socketpair()
    try:
        left.sendall(cli_access.masked_client_frame(b"ssh bytes"))
        assert cli_access.read_client_frame(right) == (2, b"ssh bytes")
    finally:
        left.close()
        right.close()


def test_websocket_relay_is_byte_exact_in_both_directions():
    client, broker_client = socket.socketpair()
    broker_upstream, upstream = socket.socketpair()
    worker = threading.Thread(target=cli_access.relay_websocket,
                              args=(broker_client, broker_upstream), daemon=True)
    worker.start()
    try:
        client.sendall(cli_access.masked_client_frame(b"from-client"))
        assert upstream.recv(64) == b"from-client"
        upstream.sendall(b"from-slot")
        head = client.recv(2)
        assert head == bytes((0x82, len(b"from-slot")))
        assert client.recv(len(b"from-slot")) == b"from-slot"
        client.sendall(cli_access.masked_client_frame(b"", opcode=8))
    finally:
        client.close()
        upstream.close()
        worker.join(timeout=2)


def test_revocation_closes_an_already_open_websocket_relay():
    client, broker_client = socket.socketpair()
    broker_upstream, upstream = socket.socketpair()
    allowed = threading.Event()
    allowed.set()
    worker = threading.Thread(
        target=cli_access.relay_websocket,
        args=(broker_client, broker_upstream, allowed.is_set), daemon=True)
    worker.start()
    try:
        allowed.clear()
        worker.join(timeout=3)
        assert not worker.is_alive()
        assert client.recv(1) == b""
    finally:
        client.close()
        upstream.close()


def test_the_installed_terminal_client_does_not_read_an_anthropic_credential(tmp_path):
    root = Path(__file__).parents[1]
    client = root / "laptop" / "ccfleet"
    text = client.read_text()
    assert ".credentials.json" not in text
    assert "refreshToken" not in text
    assert "accessToken" not in text
    env = {**os.environ, "CCFLEET_HOME": str(tmp_path / "ccfleet")}
    result = subprocess.run([str(client), "list"], env=env, capture_output=True, text=True,
                            timeout=10)
    assert result.returncode == 0 and "No slots connected" in result.stdout


def test_slot_entry_forces_the_persistent_original_claude_session():
    text = (Path(__file__).parents[1] / "node" / "slot-entry.sh").read_text()
    assert "SSH_ORIGINAL_COMMAND" in text
    assert "ccfleet-session" in text and "eval" not in text
    for mode in ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"):
        assert mode in text
    for effort in ("low", "medium", "high", "xhigh", "max", "ultracode"):
        assert effort in text
    assert 'tmux new-session -A -s "$SESSION"' in text
    assert '"$HOME/.local/bin/claude"' in text
    assert "--dangerously-skip-permissions" in text
    assert '--model "$MODEL"' in text and '--effort "$EFFORT"' in text
    assert "MODEL=${MODEL:-default}" in text and "EFFORT=${EFFORT:-default}" in text
    assert 'if [ "$SESSION" = ccfleet ]' not in text, \
        "restarting the default must honor the requested launch choices too"


def run_slot_entry(tmp_path, request):
    home = tmp_path / "home"
    fakebin = tmp_path / "bin"
    home.mkdir()
    fakebin.mkdir()
    log = tmp_path / "tmux-args"
    tmux = fakebin / "tmux"
    tmux.write_text("""#!/bin/sh
if [ "$1" = has-session ]; then
  [ "$3" = ccfleet ] && exit 0
  exit 1
fi
printf '%s\\n' "$@" > "$CCFLEET_TEST_TMUX_ARGS"
""")
    tmux.chmod(0o755)
    entry = Path(__file__).parents[1] / "node" / "slot-entry.sh"
    pid, fd = pty.fork()
    if pid == 0:
        env = {**os.environ, "HOME": str(home),
               "PATH": str(fakebin) + os.pathsep + os.environ["PATH"],
               "SSH_ORIGINAL_COMMAND": request,
               "CCFLEET_TEST_TMUX_ARGS": str(log)}
        os.execve(entry, [str(entry)], env)
    _, status = os.waitpid(pid, 0)
    os.close(fd)
    return os.waitstatus_to_exitcode(status), log.read_text().splitlines()


def test_slot_entry_passes_validated_model_and_effort_as_separate_arguments(tmp_path):
    code, args = run_slot_entry(
        tmp_path, "ccfleet-session new research bypassPermissions fable ultracode")
    assert code == 0
    assert args[-5:] == [
        "--dangerously-skip-permissions", "--model", "fable", "--effort", "ultracode"]


def test_slot_entry_keeps_the_previous_four_field_client_protocol_working(tmp_path):
    code, args = run_slot_entry(tmp_path, "ccfleet-session new legacy plan")
    assert code == 0
    assert args[-2:] == ["--permission-mode", "plan"]
    assert "--model" not in args and "--effort" not in args


@pytest.fixture
def local_client():
    return runpy.run_path(str(Path(__file__).parents[1] / "laptop" / "ccfleet"))


def test_a_long_lived_session_gets_a_fresh_reconnect_window(local_client, monkeypatch):
    clock = {"now": 0.0}
    calls = []

    def ssh_call(_argv):
        calls.append(clock["now"])
        if len(calls) == 1:
            clock["now"] += 3_600
            return 255
        return 0

    globals_ = local_client["cmd_attach"].__globals__
    monkeypatch.setitem(globals_, "load_config", lambda: {"devices": {}})
    monkeypatch.setitem(globals_, "choose_device", lambda *_: {"slot_name": "slot-1"})
    monkeypatch.setitem(globals_, "ssh_command", lambda *_args: ["ssh"])
    monkeypatch.setattr(subprocess, "call", ssh_call)
    monkeypatch.setattr(local_client["time"], "monotonic", lambda: clock["now"])
    monkeypatch.setattr(local_client["time"], "sleep",
                        lambda seconds: clock.__setitem__("now", clock["now"] + seconds))

    result = local_client["cmd_attach"](
        SimpleNamespace(slot="", session="ccfleet", mode="bypassPermissions", action="open",
                        model="opus", effort="max", no_reconnect=False, reconnect_for=600))

    assert result == 0 and len(calls) == 2


def test_local_ssh_command_disables_every_forwarding_path(local_client, monkeypatch):
    monkeypatch.setattr(local_client["shutil"], "which", lambda _name: "/usr/bin/ssh")
    device = {"device_id": "d1", "host_alias": "ccfleet-s1", "known_hosts": "/k",
              "key": "/i", "user": "slot01"}
    command = local_client["ssh_command"](device)
    joined = " ".join(command)
    for option in ("ClearAllForwardings=yes", "ForwardAgent=no", "ForwardX11=no",
                   "PermitLocalCommand=no", "StrictHostKeyChecking=yes", "UpdateHostKeys=no"):
        assert option in joined
    assert command[-6:] == ["ccfleet-session", "open", "ccfleet", "bypassPermissions",
                            "opus", "max"]


def test_named_session_choices_are_a_fixed_remote_protocol(local_client, monkeypatch):
    monkeypatch.setattr(local_client["shutil"], "which", lambda _name: "/usr/bin/ssh")
    device = {"device_id": "d1", "host_alias": "ccfleet-s1", "known_hosts": "/k",
              "key": "/i", "user": "slot01"}
    command = local_client["ssh_command"](
        device, "research_1", "plan", "new", "fable", "ultracode")
    assert command[-6:] == [
        "ccfleet-session", "new", "research_1", "plan", "fable", "ultracode"]


def test_bad_session_names_are_refused_before_reading_local_config(local_client):
    assert local_client["main"](["new", "../shell"]) == 2


def test_bad_model_names_are_refused_before_reading_local_config(local_client):
    assert local_client["main"](["new", "research", "--model", "opus;touch-pwned"]) == 2


def test_real_claude_effort_levels_are_offered_by_the_client(local_client):
    parser = local_client["parser"]()
    args = parser.parse_args([
        "new", "research", "--model", "fable", "--effort", "ultracode"])
    assert (args.model, args.effort, args.mode) == ("fable", "ultracode", "bypassPermissions")
    with pytest.raises(SystemExit):
        parser.parse_args(["new", "research", "--effort", "extreme-max"])


def test_device_secrets_are_never_sent_over_plaintext_websockets(local_client):
    with pytest.raises(local_client["CliError"]):
        local_client["open_websocket"]("ws://fleet.example.com/api/cli/connect", "secret")


@pytest.fixture
def handshake_peer(local_client, monkeypatch):
    class Peer:
        def __init__(self, transform=lambda headers: headers, status="HTTP/1.1 101 Switching Protocols"):
            self.transform, self.status = transform, status
            self.closed, self.timeouts, self.recv_calls = False, [], 0

        def settimeout(self, timeout):
            self.timeouts.append(timeout)

        def sendall(self, request):
            fields = dict(line.split(": ", 1) for line in request.decode().split("\r\n")[1:]
                          if ": " in line)
            accept = cli_access.websocket_accept(fields["Sec-WebSocket-Key"])
            headers = self.transform(["Upgrade: websocket", "Connection: Upgrade",
                                      "Sec-WebSocket-Accept: " + accept])
            self.response = (self.status + "\r\n" + "\r\n".join(headers)
                             + "\r\n\r\n").encode() + b"initial SSH frame"

        def recv(self, _size):
            self.recv_calls += 1
            return self.response

        def close(self):
            self.closed = True

    def create(*args, **kwargs):
        peer = Peer(*args, **kwargs)
        monkeypatch.setattr(local_client["socket"], "create_connection", lambda *_a, **_k: peer)
        return peer

    return create


def test_websocket_upgrade_preserves_initial_frame_and_removes_timeout_only_when_ready(
        local_client, handshake_peer):
    peer = handshake_peer(lambda headers: [headers[0], "Connection: keep-alive, UpGrAdE", headers[2]])
    sock, initial = local_client["open_websocket"]("ws://127.0.0.1:9000/connect", "synthetic")
    assert sock is peer and initial == b"initial SSH frame" and not peer.closed
    assert peer.timeouts[-1] is None
    assert all(0 < value <= 15 for value in peer.timeouts[:-1])


@pytest.mark.parametrize("transform", [
    lambda headers: headers[1:],
    lambda headers: [headers[0], headers[2]],
    lambda headers: ["Upgrade: h2c", *headers[1:]],
    lambda headers: [headers[0], "Connection: not-upgrade", headers[2]],
    lambda headers: [*headers, "connection: Upgrade"],
    lambda headers: [*headers, "upgrade: websocket"],
    lambda headers: [*headers, headers[2]],
    lambda headers: [*headers, "Sec-WebSocket-Extensions: permessage-deflate"],
    lambda headers: [*headers, "Sec-WebSocket-Protocol: unexpected"],
    lambda headers: [*headers, " folded-header: value"],
    lambda headers: [*headers, "malformed header"],
    lambda headers: [headers[0], headers[1], "Sec-WebSocket-Accept: incorrect"],
])
def test_websocket_upgrade_rejects_ambiguous_or_unnegotiated_headers(
        local_client, handshake_peer, transform):
    peer = handshake_peer(transform)
    with pytest.raises(local_client["CliError"]):
        local_client["open_websocket"]("ws://127.0.0.1/connect", "synthetic")
    assert peer.closed and None not in peer.timeouts


def test_websocket_upgrade_rejects_non_switching_status(local_client, handshake_peer):
    peer = handshake_peer(status="HTTP/1.1 200 OK")
    with pytest.raises(local_client["CliError"], match="refused"):
        local_client["open_websocket"]("ws://127.0.0.1/connect", "synthetic")
    assert peer.closed


@pytest.mark.parametrize("status", ["HTTP/1.1 401 SECRET_credential\x1b[31m",
                                    "invalid SECRET_credential response"])
def test_websocket_refusal_never_echoes_remote_reason(local_client, handshake_peer, status):
    peer = handshake_peer(status=status)
    with pytest.raises(local_client["CliError"], match="refused") as error:
        local_client["open_websocket"]("ws://127.0.0.1/connect", "synthetic")
    assert "SECRET" not in str(error.value) and "\x1b" not in str(error.value)
    assert peer.closed


def test_websocket_trickled_headers_share_one_absolute_deadline(
        local_client, handshake_peer, monkeypatch):
    peer = handshake_peer()
    now = [0.0]
    globals_ = local_client["open_websocket"].__globals__
    monkeypatch.setitem(globals_, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def slow_receive(_size):
        peer.recv_calls += 1
        now[0] += 6
        return b"x"

    monkeypatch.setattr(peer, "recv", slow_receive)
    with pytest.raises(local_client["CliError"], match="handshake timed out"):
        local_client["open_websocket"]("ws://127.0.0.1/connect", "synthetic")
    assert peer.recv_calls == 3 and peer.closed
    assert peer.timeouts == [15, 15, 9, 3]


def test_secure_websocket_verifies_broker_hostname(local_client, handshake_peer, monkeypatch):
    peer = handshake_peer()
    contexts, hostnames = [], []
    real_context = ssl_context = local_client["ssl"].create_default_context()
    assert real_context.check_hostname
    assert real_context.verify_mode == local_client["ssl"].CERT_REQUIRED

    def context():
        contexts.append(ssl_context)
        return SimpleNamespace(wrap_socket=lambda sock, server_hostname:
                               hostnames.append(server_hostname) or sock)

    monkeypatch.setattr(local_client["ssl"], "create_default_context", context)
    sock, _ = local_client["open_websocket"]("wss://broker.example/connect", "synthetic")
    assert sock is peer and len(contexts) == 1 and hostnames == ["broker.example"]


@pytest.fixture
def proxy_peer():
    sock, peer = socket.socketpair()
    input_fd, input_writer = os.pipe()
    output_reader, output_fd = os.pipe()
    try:
        yield sock, input_fd, output_fd
    finally:
        sock.close()
        peer.close()
        for descriptor in (input_fd, input_writer, output_reader, output_fd):
            os.close(descriptor)


def test_proxy_preserves_all_ssh_bytes_when_pipe_writes_are_partial(
        local_client, proxy_peer, monkeypatch):
    written = []
    sock, input_fd, output_fd = proxy_peer

    def short_write(fd, data):
        assert fd == output_fd
        written.append(bytes(data[:2]))
        return len(written[-1])

    monkeypatch.setattr(local_client["os"], "write", short_write)
    initial = cli_access.websocket_frame(b"abcdefgh") + cli_access.websocket_frame(b"", 8)
    assert local_client["_proxy_stream"](sock, initial, input_fd=input_fd,
                                        output_fd=output_fd) == 0
    assert b"".join(written) == b"abcdefgh" and len(written) == 4
    assert os.get_blocking(input_fd) and os.get_blocking(output_fd)


def test_proxy_fails_instead_of_spinning_when_pipe_write_makes_no_progress(
        local_client, proxy_peer, monkeypatch):
    sock, input_fd, output_fd = proxy_peer
    monkeypatch.setattr(local_client["os"], "write", lambda *_: 0)
    with pytest.raises(local_client["CliError"], match="SSH input closed"):
        local_client["_proxy_stream"](sock, cli_access.websocket_frame(b"abcdefgh"),
                                      input_fd=input_fd, output_fd=output_fd)
    assert os.get_blocking(input_fd) and os.get_blocking(output_fd)
    assert sock.fileno() == -1


def test_proxy_uses_one_validated_connection_without_reconnecting(local_client, monkeypatch):
    scope = local_client["proxy"].__globals__
    peer, calls = object(), []

    def connected(endpoint, token):
        calls.append((endpoint, token))
        return peer, b"initial SSH frame"

    def stream(sock, initial):
        assert sock is peer and initial == b"initial SSH frame"
        return 0

    monkeypatch.setitem(scope, "open_websocket", connected)
    monkeypatch.setitem(scope, "_proxy_stream", stream)
    assert local_client["proxy"]({"endpoint": "unused", "device_token": "synthetic"}) == 0
    assert calls == [("unused", "synthetic")]


@pytest.mark.parametrize("wire", [
    b"\x02\x00", b"\xc2\x00", b"\x82\x80", b"\x81\x00", b"\x88\x01X",
    b"\x89\x7e\x00\x7e",
    b"\x82\x7f" + (2 * 1024 * 1024 + 1).to_bytes(8, "big"),
])
def test_proxy_rejects_invalid_or_oversized_frames_before_buffering_payload(
        local_client, proxy_peer, wire):
    sock, input_fd, output_fd = proxy_peer
    with pytest.raises(local_client["CliError"]) as failure:
        local_client["_proxy_stream"](sock, wire + b"PRIVATE_SYNTHETIC_FRAME_TEXT",
                                      input_fd=input_fd, output_fd=output_fd)
    assert "PRIVATE" not in str(failure.value)
    assert os.get_blocking(input_fd) and os.get_blocking(output_fd)
    assert sock.fileno() == -1


def test_proxy_does_not_accept_eof_inside_a_partial_websocket_frame(local_client):
    sock, peer = socket.socketpair()
    input_fd, input_writer = os.pipe()
    output_reader, output_fd = os.pipe()
    try:
        peer.sendall(b"\x82")
        peer.shutdown(socket.SHUT_WR)
        with pytest.raises(local_client["CliError"], match="closed during a WebSocket frame"):
            local_client["_proxy_stream"](sock, input_fd=input_fd, output_fd=output_fd)
        assert os.get_blocking(input_fd) and os.get_blocking(output_fd)
        assert sock.fileno() == -1
    finally:
        sock.close()
        peer.close()
        for descriptor in (input_fd, input_writer, output_reader, output_fd):
            os.close(descriptor)
