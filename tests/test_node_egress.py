"""Synthetic CONNECT traffic: no model, vendor endpoint or user credential is used."""
from __future__ import annotations

import base64
import json
import os
import socket
import socketserver
import ssl
import threading

import pytest

from ccfleet_agent import egress, local_relay


@pytest.fixture
def policy(tmp_path, monkeypatch):
    directory = tmp_path / "policy"
    directory.mkdir(mode=0o755)
    monkeypatch.setattr(egress, "ROOT_UID", os.getuid())
    monkeypatch.setattr(egress, "POLICY_FILE", directory / "egress.json")
    monkeypatch.setattr(egress, "UPSTREAM_FILE", directory / "upstream.json")
    egress.POLICY_FILE.write_text('{"version":1,"enabled":true}')
    egress.POLICY_FILE.chmod(0o644)
    return directory


class Upstream(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        self.server.headers.append(egress._headers(self.request))
        if self.server.reject:
            self.request.sendall(b"HTTP/1.1 407 PRIVATE_PROXY_PASSWORD\r\n"
                                 b"Proxy-Authenticate: PRIVATE_UPSTREAM\r\n\r\n")
            return
        self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        while True:
            data = self.request.recv(65536)
            if not data:
                self.server.disconnected.set()
                return
            self.request.sendall(data)


@pytest.fixture
def proxies(policy, monkeypatch):
    upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Upstream)
    upstream.daemon_threads = True
    upstream.headers, upstream.reject = [], False
    upstream.disconnected = threading.Event()
    config = {"version": 1, "scheme": "http", "host": "127.0.0.1",
              "port": upstream.server_address[1], "username": "PRIVATE_USER",
              "password": "PRIVATE_PASSWORD", "allowed_hosts": ["api.anthropic.com"]}
    egress.UPSTREAM_FILE.write_text(json.dumps(config))
    egress.UPSTREAM_FILE.chmod(0o600)
    proxy = egress.ProxyServer(("127.0.0.1", 0))
    monkeypatch.setattr(egress, "LISTEN_PORT", proxy.server_address[1])
    threads = [threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01},
                                daemon=True) for server in (upstream, proxy)]
    for thread in threads:
        thread.start()
    yield proxy, upstream
    for server in (proxy, upstream):
        server.shutdown()
        server.server_close()
    for thread in threads:
        thread.join(timeout=2)


@pytest.mark.parametrize("version", ["1.0", "1.1"])
def test_real_connect_keeps_proxy_auth_private_and_stream_bytes_unchanged(proxies, version):
    proxy, upstream = proxies
    with socket.create_connection(proxy.server_address, timeout=3) as client:
        client.sendall(f"CONNECT api.anthropic.com:443 HTTP/{version}\r\n"
                       "Host: api.anthropic.com:443\r\n\r\n".encode())
        headers = egress._headers(client)
        assert headers == [b"HTTP/1.1 200 Connection Established"]
        expected = base64.b64encode(b"PRIVATE_USER:PRIVATE_PASSWORD")
        assert upstream.headers == [[b"CONNECT api.anthropic.com:443 HTTP/1.1",
                                     b"Host: api.anthropic.com:443",
                                     b"Proxy-Authorization: Basic " + expected]]
        # Opaque inner TLS records travel unchanged through the CONNECT tunnel.
        for record in (b"\x16\x03\x03\x00\x04ABCD", b"stream part two", b"\x00\xffEND"):
            client.sendall(record)
            assert client.recv(65536) == record
    assert upstream.disconnected.wait(2), "client cancellation did not close upstream"


def test_proxy_refusal_never_reflects_credentials_or_attempts_direct_target(proxies, monkeypatch):
    proxy, upstream = proxies
    upstream.reject = True
    original = socket.create_connection
    destinations = []
    def connect(address, *args, **kwargs):
        destinations.append(address)
        return original(address, *args, **kwargs)
    monkeypatch.setattr(egress.socket, "create_connection", connect)
    with original(proxy.server_address, timeout=3) as client:
        client.sendall(b"CONNECT api.anthropic.com:443 HTTP/1.0\r\n\r\n")
        response = client.recv(65536)
    assert response == egress._FAILURE and b"PRIVATE" not in response
    assert destinations == [upstream.server_address]


def test_missing_private_configuration_cannot_fall_back_to_direct(proxies):
    proxy, upstream = proxies
    egress.UPSTREAM_FILE.unlink()
    with socket.create_connection(proxy.server_address, timeout=3) as client:
        client.sendall(b"CONNECT api.anthropic.com:443 HTTP/1.0\r\n\r\n")
        assert client.recv(65536) == egress._FAILURE
    assert upstream.headers == [] and egress.native_environment()["HTTPS_PROXY"] == egress.PROXY_URL


def test_relay_connect_uses_loopback_but_preserves_provider_tls_identity(policy, monkeypatch):
    class Stream:
        def close(self):
            pass
    stream, calls = Stream(), []
    context = ssl.create_default_context()
    monkeypatch.setattr(egress.socket, "create_connection", lambda address, timeout:
                        calls.append((address, timeout)) or stream)
    monkeypatch.setattr(egress.http.client.HTTPConnection, "_tunnel",
                        lambda self: calls.append((self._tunnel_host, self._tunnel_port)))
    monkeypatch.setattr(context, "wrap_socket", lambda sock, server_hostname:
                        calls.append((sock, server_hostname)) or sock)
    connection = egress.provider_connection("api.anthropic.com", 443, timeout=20, context=context)
    assert connection.host == "api.anthropic.com" and connection.port == 443
    connection.connect()
    assert calls == [(("127.0.0.1", 912), 20), ("api.anthropic.com", 443),
                     (stream, "api.anthropic.com")]
    assert connection._context.check_hostname and connection._context.verify_mode == ssl.CERT_REQUIRED
    connection.close()


def test_loopback_connect_failure_is_redacted_and_has_no_second_route(policy, monkeypatch):
    calls = []
    def refused(address, timeout):
        calls.append(address)
        raise OSError("PRIVATE_PROXY_SECRET")
    monkeypatch.setattr(egress.socket, "create_connection", refused)
    with pytest.raises(egress.EgressError) as caught:
        local_relay.connect_upstream().connect()
    assert str(caught.value) == egress.ERROR and calls == [("127.0.0.1", 912)]


def test_policy_removed_between_constructor_and_connect_never_opens_any_socket(policy, monkeypatch):
    connection = local_relay.connect_upstream()
    egress.POLICY_FILE.unlink()
    monkeypatch.setattr(egress.socket, "create_connection",
                        lambda *a, **k: pytest.fail("missing opt-in policy opened a socket"))
    with pytest.raises(egress.EgressError):
        connection.connect()


def test_https_upstream_uses_verified_proxy_tls_before_connect_auth(policy, monkeypatch):
    calls = []
    class Stream:
        def __init__(self):
            self.response = iter(b"HTTP/1.1 200 OK\r\n\r\n")
        def settimeout(self, _):
            pass
        def sendall(self, data):
            calls.append(("send", data))
        def recv(self, _):
            return bytes([next(self.response)])
        def close(self):
            pass
    stream = Stream()
    context = ssl.create_default_context()
    monkeypatch.setattr(context, "wrap_socket", lambda sock, server_hostname:
                        calls.append(("verified_tls", server_hostname)) or sock)
    monkeypatch.setattr(egress.ssl, "create_default_context", lambda: context)
    monkeypatch.setattr(egress.socket, "create_connection", lambda address, timeout:
                        calls.append(("connect", address)) or stream)
    assert egress._upstream({"scheme": "https", "host": "proxy.example", "port": 443,
                             "username": "name", "password": "secret"},
                            "api.anthropic.com") is stream
    assert calls[0] == ("connect", ("proxy.example", 443))
    assert calls[1] == ("verified_tls", "proxy.example")
    assert calls[2][0] == "send" and b"Proxy-Authorization: Basic " in calls[2][1]
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED


def test_managed_settings_persist_only_credential_free_route_and_leave_other_policy_files(policy):
    directory = policy / "managed-settings.d"
    directory.mkdir()
    other = directory / "10-other.json"
    other.write_text('{"permissions":{"defaultMode":"default"}}')
    target = directory / "90-ccfleet-egress.json"
    assert egress.install_native_settings(target)
    assert json.loads(target.read_text()) == {"env": egress.native_environment()}
    assert other.read_text() == '{"permissions":{"defaultMode":"default"}}'
    assert "PRIVATE" not in target.read_text()
    assert not list(directory.glob("*.tmp.*"))


def test_current_signed_laptop_bundle_matches_its_release():
    import ast
    import hashlib
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    source = ast.parse((root / "laptop/ccfleet").read_bytes())
    version = next(node.value.value for node in source.body
                   if isinstance(node, ast.Assign) and len(node.targets) == 1
                   and isinstance(node.targets[0], ast.Name)
                   and node.targets[0].id == "CLIENT_VERSION")
    manifest = json.loads((root / ("laptop/releases/" + version + ".json")).read_text())
    assert manifest["version"] == version
    for item in manifest["files"]:
        assert hashlib.sha256((root / item["path"]).read_bytes()).hexdigest() == item["sha256"]


def test_previous_025_release_metadata_stays_immutable():
    import hashlib
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "laptop/releases"
    expected = {
        "0.2.5.json": "677901131f004dceaed91e1a9add78701afa7d358ce32d833e9474347945bde2",
        "0.2.5.json.sig": "68589d18f5c4297bbe7f76dda931e5ed8ffe73af63b44a28e6f434a09e27490c",
    }
    for name, digest in expected.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("threshold", ["0", "912", "not-a-number", "65537"])
def test_daemon_refuses_listener_that_a_holder_could_impersonate(policy, monkeypatch,
                                                               threshold, capsys):
    sysctl = policy / "unprivileged-port-start"
    sysctl.write_text(threshold)
    monkeypatch.setattr(egress, "UNPRIVILEGED_PORT_FILE", sysctl)
    monkeypatch.setattr(egress, "upstream_config", lambda: {})
    monkeypatch.setattr(egress.os, "geteuid", lambda: egress.ROOT_UID)
    monkeypatch.setattr(egress, "ProxyServer", lambda: pytest.fail("bound unsafe listener"))
    assert egress.main(["serve"]) == 1
    assert capsys.readouterr().err == egress.ERROR + "\n"


def test_connect_header_budget_is_absolute_not_renewed_by_slow_bytes(monkeypatch):
    ticks = iter(range(1000))
    monkeypatch.setattr(egress.time, "monotonic", lambda: next(ticks))
    class SlowHeaders:
        def __init__(self):
            self.timeouts = []
        def settimeout(self, value):
            self.timeouts.append(value)
        def recv(self, _):
            return b"x"
    stream = SlowHeaders()
    with pytest.raises(egress.EgressError):
        egress._headers(stream)
    assert stream.timeouts == list(range(egress.CONNECT_TIMEOUT - 1, 0, -1))


class Tunnel:
    def __init__(self, incoming):
        self.incoming = iter(incoming)
        self.sent = []
        self.timeout = None
    def settimeout(self, value):
        self.timeout = value
    def recv(self, _):
        return next(self.incoming)
    def sendall(self, data):
        self.sent.append(data)


def test_progressing_native_tunnel_reuse_continues_beyond_fifteen_minutes(monkeypatch):
    timestamps = iter([0, 0, 0, 200, 200, 400, 400, 600, 600, 800, 800, 1000, 1000, 1200])
    monkeypatch.setattr(egress.time, "monotonic", lambda: next(timestamps))
    client = Tunnel([b"reply"] * 6 + [b""])
    upstream = Tunnel([])
    monkeypatch.setattr(egress.select, "select", lambda *args: ([client], [], []))
    egress._pump(client, upstream)
    assert upstream.sent == [b"reply"] * 6
    assert client.timeout == upstream.timeout == egress.IDLE_TIMEOUT


@pytest.mark.parametrize("idle", [False, True])
def test_tunnel_stops_on_eof_or_idle_without_writing_extra_bytes(monkeypatch, idle):
    timestamps = iter([0, egress.IDLE_TIMEOUT + 1 if idle else 0])
    monkeypatch.setattr(egress.time, "monotonic", lambda: next(timestamps))
    client, upstream = Tunnel([b""]), Tunnel([])
    def readable(*args):
        assert not idle, "idle tunnel should not start another read"
        return [client], [], []
    monkeypatch.setattr(egress.select, "select", readable)
    egress._pump(client, upstream)
    assert client.sent == upstream.sent == []
