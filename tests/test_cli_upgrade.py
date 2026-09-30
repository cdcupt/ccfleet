"""Real direct-loopback WebSocket upgrades, with synthetic auth and byte peers."""

from __future__ import annotations

import contextlib
import http.client
import json
import runpy
import socket
import socketserver
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleetd import cli_access
from ccfleetd.api import Context, build_server
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.store import Store
from tests.test_cli_access import active_slot, public_key

GREETING = b"SSH-2.0-synthetic-fixture\r\n"
PAYLOAD = b"\x00\xffopaque-inner-ssh-bytes\r\n"


@pytest.fixture(params=[False, True], ids=["primary", "broker-only"])
def direct_broker(cfg, request):
    received = []
    disconnected = threading.Event()
    connections = []

    class Node(socketserver.BaseRequestHandler):
        def handle(self):
            connections.append(self.request)
            try:
                self.request.settimeout(5)
                self.request.sendall(GREETING)
                while True:
                    data = self.request.recv(65536)
                    if not data:
                        break
                    received.append(data)
                    self.request.sendall(data)
            except OSError:
                pass
            finally:
                disconnected.set()

    node = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Node)
    node.daemon_threads = True
    node_thread = threading.Thread(target=node.serve_forever, daemon=True)
    node_thread.start()
    store = Store(":memory:")
    slot = active_slot(store)
    store.set_node_access("m1", "127.0.0.1", node.server_address[1], public_key(9))
    code = store.request_cli_pairing(slot["id"], "a1", now=time.time())
    device = store.register_cli_device(code, public_key(), "synthetic", now=time.time())
    configured = replace(cfg, broker_only=request.param)
    server = build_server(Context(store, configured, Monitor(store, configured, LogNotifier())),
                          host="127.0.0.1", port=0)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    client = runpy.run_path(str(Path(__file__).parents[1] / "laptop/ccfleet"))
    result = SimpleNamespace(server=server, store=store, slot=slot, device=device, client=client,
                             node=node, received=received, connections=connections,
                             disconnected=disconnected,
                             endpoint=f"ws://127.0.0.1:{server.server_address[1]}/api/cli/connect")
    try:
        yield result
    finally:
        for connection in connections:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            connection.close()
        server.shutdown()
        server.server_close()
        serving.join(timeout=3)
        node.shutdown()
        node.server_close()
        node_thread.join(timeout=3)
        store.close()


def rest(fixture, method, path, headers=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", fixture.server.server_address[1], timeout=3)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.version, response.will_close, response.read()
    finally:
        connection.close()


def open_channel(fixture):
    # Exercise the unchanged, strict production client against a real server,
    # without a reverse proxy upgrading an incorrect HTTP/1.0 status line.
    connection, initial = fixture.client["open_websocket"](
        fixture.endpoint, fixture.device["device_token"])
    connection.settimeout(3)
    return connection, fixture.client["FrameReader"](connection, initial)


def receive_bytes(reader, count):
    output = bytearray()
    while len(output) < count:
        opcode, payload = reader.read()
        assert opcode == 2
        output.extend(payload)
    return bytes(output)


def assert_closed(reader):
    try:
        opcode, _ = reader.read()
        assert opcode == 8
    except (EOFError, ConnectionResetError):
        pass


@pytest.mark.parametrize("termination", ["revoke", "release"])
def test_direct_http11_upgrade_relays_bytes_and_closes_on_revocation_or_release(direct_broker,
                                                                              termination):
    fixture = direct_broker
    assert rest(fixture, "GET", "/healthz")[:3] == (200, 10, True)
    connection, reader = open_channel(fixture)
    try:
        assert receive_bytes(reader, len(GREETING)) == GREETING
        connection.sendall(cli_access.masked_client_frame(PAYLOAD))
        assert receive_bytes(reader, len(PAYLOAD)) == PAYLOAD
        assert b"".join(fixture.received) == PAYLOAD
        assert len(fixture.connections) == 1
        # A concurrent REST request and the class default must remain HTTP/1.0.
        assert rest(fixture, "GET", "/healthz")[:3] == (200, 10, True)
        assert fixture.server.RequestHandlerClass.protocol_version == "HTTP/1.0"
        if termination == "revoke":
            status, version, close, raw = rest(
                fixture, "POST", "/api/cli/revoke", body=b"{}",
                headers={"Authorization": "Bearer " + fixture.device["device_token"],
                         "Content-Type": "application/json"})
            assert (status, version, close) == (200, 10, True)
            assert json.loads(raw) == {"ok": True}
        else:
            fixture.store.begin_release(fixture.slot["id"], held_by="a1")
        assert_closed(reader)
        assert fixture.disconnected.wait(timeout=3)
        with pytest.raises(fixture.client["CliError"], match="HTTP 401"):
            open_channel(fixture)
        assert len(fixture.connections) == 1, "revoked devices must not reach the configured node"
        assert rest(fixture, "GET", "/healthz")[:3] == (200, 10, True)
    finally:
        connection.close()


def test_direct_upgrade_uses_only_operator_endpoint_not_request_parameters(direct_broker):
    fixture = direct_broker
    with socket.socket() as trap:
        trap.bind(("127.0.0.1", 0))
        trap.listen(1)
        trap.settimeout(0.1)
        fixture.endpoint += f"?host=127.0.0.1&port={trap.getsockname()[1]}"
        connection, reader = open_channel(fixture)
        try:
            assert receive_bytes(reader, len(GREETING)) == GREETING
            assert len(fixture.connections) == 1
            with pytest.raises(socket.timeout):
                trap.accept()
            connection.sendall(cli_access.masked_client_frame(b"", opcode=8))
        finally:
            connection.close()


@pytest.mark.parametrize("change,status", [
    ({"Authorization": "Bearer synthetic-invalid-token"}, 401),
    ({"Upgrade": "not-websocket"}, 426),
    ({"Connection": "close"}, 426),
    ({"Sec-WebSocket-Version": "12"}, 426),
    ({"Sec-WebSocket-Key": "not-base64"}, 400),
])
def test_direct_upgrade_errors_stay_http10_and_never_connect_upstream(direct_broker, change, status):
    fixture = direct_broker
    headers = {"Authorization": "Bearer " + fixture.device["device_token"],
               "Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Version": "13",
               "Sec-WebSocket-Key": "MDEyMzQ1Njc4OWFiY2RlZg==", **change}
    assert rest(fixture, "GET", "/api/cli/connect", headers=headers)[:3] == (status, 10, True)
    assert fixture.connections == []


def test_protocol_override_is_restored_before_connection_handler_returns(direct_broker, monkeypatch):
    fixture = direct_broker
    observed = []
    finished = threading.Event()
    handler = fixture.server.RequestHandlerClass
    original = handler.handle_one_request

    def handle(self):
        try:
            original(self)
        finally:
            observed.append((self.protocol_version, self.close_connection))
            finished.set()

    monkeypatch.setattr(handler, "handle_one_request", handle)
    connection, reader = open_channel(fixture)
    try:
        assert receive_bytes(reader, len(GREETING)) == GREETING
        connection.sendall(cli_access.masked_client_frame(b"", opcode=8))
        assert finished.wait(timeout=3)
        assert observed == [("HTTP/1.0", True)]
    finally:
        connection.close()


def test_http10_upgrade_wire_mutation_is_rejected_by_unchanged_strict_client(direct_broker,
                                                                          monkeypatch):
    """Restore the old status-line bug in memory; the real client must reject it."""
    fixture = direct_broker
    handler = fixture.server.RequestHandlerClass
    original = handler.send_response_only

    def old_status_line(self, code, message=None):
        saved = self.protocol_version
        try:
            if code == 101:
                self.protocol_version = "HTTP/1.0"
            return original(self, code, message)
        finally:
            self.protocol_version = saved

    monkeypatch.setattr(handler, "send_response_only", old_status_line)
    with pytest.raises(fixture.client["CliError"], match="HTTP 101"):
        open_channel(fixture)
    assert fixture.disconnected.wait(timeout=3)
