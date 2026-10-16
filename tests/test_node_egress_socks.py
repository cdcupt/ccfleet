"""Authenticated SOCKS5 framing/security tests; no real socket or credentials."""
from __future__ import annotations

import socket

import pytest

from ccfleet_agent import egress

TARGET = "api.anthropic.com"
PRIVATE = "SYNTHETIC_PRIVATE_SOCKS_SECRET"
TLS_RECORD = b"\x16\x03\x03\x00\x04TLS!"
IPV4_REPLY = b"\x05\x00\x00\x01\x7f\x00\x00\x01\x01\xbb"
HANDSHAKE = b"\x05\x02\x01\x00" + IPV4_REPLY


def config(**changes):
    return {"version": 1, "scheme": "socks5", "host": "proxy.example.test", "port": 1080,
            "username": "fixture-user", "password": PRIVATE,
            "allowed_hosts": [TARGET], **changes}


@pytest.fixture
def configured(monkeypatch):
    value = config()
    monkeypatch.setattr(egress, "enabled", lambda: True)

    def private_read(path, *, private=False):
        assert path == egress.UPSTREAM_FILE and private is True
        return value.copy()

    monkeypatch.setattr(egress, "_read", private_read)
    return value


@pytest.mark.parametrize("scheme", ["http", "https", "socks5"])
def test_supported_transport_keeps_exact_private_config_contract(configured, scheme):
    configured["scheme"] = scheme
    assert egress.upstream_config() == configured


@pytest.mark.parametrize("field,value", [
    ("scheme", "socks4"), ("scheme", "socks5h"), ("scheme", "SOCKS5"),
    ("scheme", "direct"), ("version", True), ("host", "bad host"),
    ("host", "proxy.example.test/path"), ("port", True), ("port", 0),
    ("port", 65536), ("username", ""), ("password", ""),
    ("username", "line\nname"),
    ("password", "secret\x00suffix"), ("allowed_hosts", []),
    ("allowed_hosts", [TARGET, TARGET]), ("allowed_hosts", ["127.0.0.1"]),
    ("allowed_hosts", ["*.anthropic.com"]),
])
def test_socks_config_does_not_relax_existing_input_guards(configured, field, value):
    configured[field] = value
    with pytest.raises(egress.EgressError, match="^" + egress.ERROR + "$"):
        egress.upstream_config()


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_existing_http_username_colon_rejection_is_preserved(configured, scheme):
    configured.update(scheme=scheme, username="name:ambiguous")
    with pytest.raises(egress.EgressError):
        egress.upstream_config()


def test_socks_length_framing_allows_colon_in_username(configured):
    configured["username"] = "name:unambiguous"
    assert egress.upstream_config()["username"] == "name:unambiguous"


@pytest.mark.parametrize("field", ["username", "password"])
@pytest.mark.parametrize("value", ["a" * 255, "é" * 127 + "a"])
def test_socks_credentials_accept_exactly_255_utf8_bytes(configured, field, value):
    configured[field] = value
    assert len(egress.upstream_config()[field].encode("utf-8")) == 255


@pytest.mark.parametrize("field", ["username", "password"])
@pytest.mark.parametrize("value", ["a" * 256, "é" * 128, "\ud800"])
def test_socks_credentials_reject_oversized_or_unencodable_utf8(configured, field, value):
    configured[field] = value
    with pytest.raises(egress.EgressError):
        egress.upstream_config()


def test_socks_config_rejects_implicit_fallback_setting(configured):
    configured["fallback"] = "direct"
    with pytest.raises(egress.EgressError):
        egress.upstream_config()


class FakeSocket:
    def __init__(self, incoming=HANDSHAKE, *, fragment=1, fail=None, clock=None):
        self.incoming = bytearray(incoming)
        self.fragment = fragment
        self.fail = fail
        self.clock = clock
        self.sent = []
        self.timeouts = []
        self.read_sizes = []
        self.closed = False

    def _operation(self, operation):
        if self.clock is not None:
            self.clock[0] += 0.4
        if self.fail == operation:
            raise socket.timeout(PRIVATE)

    def settimeout(self, value):
        self.timeouts.append(value)
        self._operation("settimeout")

    def sendall(self, value):
        self._operation("sendall")
        self.sent.append(bytes(value))

    def recv(self, size):
        self.read_sizes.append(size)
        self._operation("recv")
        length = min(size, self.fragment, len(self.incoming))
        chunk = bytes(self.incoming[:length])
        del self.incoming[:length]
        return chunk

    def close(self):
        self.closed = True


def route(monkeypatch, stream, *, connected=None):
    calls = []

    def connect(address, timeout):
        calls.append((address, timeout))
        if connected is not None:
            connected()
        return stream

    monkeypatch.setattr(egress.socket, "create_connection", connect)
    monkeypatch.setattr(egress.ssl, "create_default_context",
                        lambda: pytest.fail("SOCKS must preserve opaque provider TLS"))
    return calls


@pytest.mark.parametrize("reply", [
    IPV4_REPLY,
    b"\x05\x00\x00\x03\x01x\x01\xbb",
    b"\x05\x00\x00\x03\xff" + b"x" * 255 + b"\x01\xbb",
    b"\x05\x00\x00\x04" + b"\x00" * 15 + b"\x01\x01\xbb",
])
@pytest.mark.parametrize("fragment", [1, 2, 1024])
def test_authenticated_connect_consumes_only_bounded_reply_then_leaves_tls(
        monkeypatch, reply, fragment):
    stream = FakeSocket(b"\x05\x02\x01\x00" + reply + TLS_RECORD, fragment=fragment)
    calls = route(monkeypatch, stream)
    value = config(username="é" * 127 + "a", password="密" * 85)
    assert egress._upstream(value, TARGET) is stream
    assert len(calls) == 1 and calls[0][0] == (value["host"], 1080)
    assert 0 < calls[0][1] <= egress.CONNECT_TIMEOUT
    user, password = value["username"].encode(), value["password"].encode()
    assert stream.sent == [b"\x05\x01\x02", b"\x01\xff" + user + b"\xff" + password,
                           b"\x05\x01\x00\x03" + bytes([len(TARGET)]) + TARGET.encode()
                           + b"\x01\xbb"]
    assert bytes(stream.incoming) == TLS_RECORD
    assert max(stream.read_sizes) <= 255
    assert not stream.closed


@pytest.mark.parametrize("prefix", [
    b"\x04\x02", b"\x05\x00", b"\x05\xff", b"\x05\x03",
    b"\x05\x02\x02\x00", b"\x05\x02\x01\x01", b"\x05\x02\x01\xff",
])
def test_refused_authentication_never_tries_unauthenticated_or_direct_route(monkeypatch, prefix):
    stream = FakeSocket(prefix + PRIVATE.encode())
    calls = route(monkeypatch, stream)
    with pytest.raises(egress.EgressError) as error:
        egress._upstream(config(), TARGET)
    assert str(error.value) == egress.ERROR and error.value.__suppress_context__
    assert len(calls) == 1 and calls[0][0] == ("proxy.example.test", 1080)
    assert stream.sent[0] == b"\x05\x01\x02"
    assert not any(TARGET.encode() in sent for sent in stream.sent)
    assert stream.closed


@pytest.mark.parametrize("reply", [
    b"\x04\x00\x00\x01", b"\x05\x00\x01\x01", b"\x05\x00\x00\x00",
    b"\x05\x00\x00\x02", b"\x05\x00\x00\xff", b"\x05\x00\x00\x03\x00",
] + [bytes([5, code, 0, 1]) for code in (*range(1, 9), 255)])
def test_malformed_or_failed_connect_reply_is_closed_and_redacted(monkeypatch, reply):
    stream = FakeSocket(b"\x05\x02\x01\x00" + reply + PRIVATE.encode())
    calls = route(monkeypatch, stream)
    with pytest.raises(egress.EgressError) as error:
        egress._upstream(config(), TARGET)
    assert str(error.value) == egress.ERROR and PRIVATE not in str(error.value)
    assert error.value.__suppress_context__ and stream.closed and len(calls) == 1


@pytest.mark.parametrize("cut", range(len(HANDSHAKE)))
def test_eof_at_each_handshake_boundary_closes_without_retry(monkeypatch, cut):
    stream = FakeSocket(HANDSHAKE[:cut])
    calls = route(monkeypatch, stream)
    with pytest.raises(egress.EgressError):
        egress._upstream(config(), TARGET)
    assert stream.closed and len(calls) == 1


@pytest.mark.parametrize("operation", ["settimeout", "sendall", "recv"])
def test_socket_failures_do_not_expose_private_exception_or_leave_socket_open(
        monkeypatch, capsys, operation):
    stream = FakeSocket(fail=operation)
    calls = route(monkeypatch, stream)
    with pytest.raises(egress.EgressError) as error:
        egress._upstream(config(), TARGET)
    assert str(error.value) == egress.ERROR and error.value.__suppress_context__
    assert stream.closed and len(calls) == 1
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_socket_cleanup_error_cannot_replace_the_generic_failure(monkeypatch):
    class BadClose(FakeSocket):
        def close(self):
            self.closed = True
            raise OSError(PRIVATE)

    stream = BadClose(b"\x05\x00")
    route(monkeypatch, stream)
    with pytest.raises(egress.EgressError) as error:
        egress._upstream(config(), TARGET)
    assert str(error.value) == egress.ERROR and error.value.__suppress_context__
    assert stream.closed


@pytest.mark.parametrize("field", ["username", "password"])
@pytest.mark.parametrize("value", [None, "", "é" * 128, "\ud800"])
def test_invalid_authentication_is_rejected_before_opening_any_socket(monkeypatch, field, value):
    stream = FakeSocket()
    calls = route(monkeypatch, stream)
    with pytest.raises(egress.EgressError):
        egress._upstream(config(**{field: value}), TARGET)
    assert calls == [] and stream.sent == []


@pytest.mark.parametrize("reply", [
    b"\x05\x00\x00\x03\xff" + b"x" * 254,
    b"\x05\x00\x00\x04" + b"x" * 15,
    IPV4_REPLY[:-1],
])
def test_truncated_address_or_port_never_returns_a_connected_tunnel(monkeypatch, reply):
    stream = FakeSocket(b"\x05\x02\x01\x00" + reply)
    calls = route(monkeypatch, stream)
    with pytest.raises(egress.EgressError):
        egress._upstream(config(), TARGET)
    assert stream.closed and len(calls) == 1


def test_tcp_connection_failure_is_generic_and_never_retried(monkeypatch):
    calls = []

    def refused(address, timeout):
        calls.append(address)
        raise OSError(PRIVATE)

    monkeypatch.setattr(egress.socket, "create_connection", refused)
    with pytest.raises(egress.EgressError) as error:
        egress._upstream(config(), TARGET)
    assert calls == [("proxy.example.test", 1080)]
    assert str(error.value) == egress.ERROR and error.value.__suppress_context__


def test_one_absolute_deadline_includes_tcp_connect_and_slow_fragmented_handshake(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(egress.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(egress, "CONNECT_TIMEOUT", 10)
    stream = FakeSocket(clock=clock)
    calls = route(monkeypatch, stream, connected=lambda: clock.__setitem__(0, 7.0))
    with pytest.raises(egress.EgressError):
        egress._upstream(config(), TARGET)
    assert calls == [(("proxy.example.test", 1080), 10)]
    assert stream.closed and bytes(stream.incoming)
    assert stream.timeouts and max(stream.timeouts) <= 3.0
    assert all(0 < value <= 3.0 for value in stream.timeouts)
    assert all(later <= earlier for earlier, later in zip(stream.timeouts, stream.timeouts[1:]))


def test_deadline_expiring_during_final_port_read_cannot_return_success(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(egress.time, "monotonic", lambda: clock[0])

    class LastReadExpired(FakeSocket):
        def recv(self, size):
            data = super().recv(size)
            if not self.incoming:
                clock[0] = float(egress.CONNECT_TIMEOUT)
            return data

    stream = LastReadExpired(fragment=1024)
    route(monkeypatch, stream)
    with pytest.raises(egress.EgressError):
        egress._upstream(config(), TARGET)
    assert stream.closed
