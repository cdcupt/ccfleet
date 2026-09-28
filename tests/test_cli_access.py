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


def test_the_installed_client_never_mentions_an_anthropic_credential(tmp_path):
    root = Path(__file__).parents[1]
    client = root / "laptop" / "ccfleet"
    text = client.read_text()
    assert "ANTHROPIC_BASE_URL" not in text
    assert "ANTHROPIC_AUTH_TOKEN" not in text
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in text
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
