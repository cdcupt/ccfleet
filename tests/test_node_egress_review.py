"""Independent egress security checks: local fixtures, no external connections."""
from __future__ import annotations

import base64
import json
import os
import ssl
from types import SimpleNamespace

import pytest

from ccfleet_agent import egress

PRIVATE = "SYNTHETIC_PRIVATE_UPSTREAM_SECRET"
TARGET = "api.anthropic.com"


def write_json(path, value, mode=0o644):
    path.write_text(json.dumps(value))
    path.chmod(mode)


@pytest.fixture
def protected(tmp_path, monkeypatch):
    directory = tmp_path.resolve() / "protected"
    directory.mkdir(mode=0o700)
    policy, upstream = directory / "egress.json", directory / "upstream.json"
    monkeypatch.setattr(egress, "ROOT_UID", os.getuid())
    monkeypatch.setattr(egress, "POLICY_FILE", policy)
    monkeypatch.setattr(egress, "UPSTREAM_FILE", upstream)
    write_json(policy, {"version": 1, "enabled": True})
    config = {"version": 1, "scheme": "https", "host": "proxy.example.test", "port": 8443,
              "username": "fixture-user", "password": PRIVATE, "allowed_hosts": [TARGET]}
    write_json(upstream, config, 0o600)
    return SimpleNamespace(directory=directory, policy=policy, upstream=upstream, config=config)


@pytest.mark.parametrize("raw", [
    b"", b"[]", b"null", b"{", b"\xff",
    b'{"version":1,"enabled":true,"unexpected":"private"}',
    b'{"version":true,"enabled":true}', b'{"version":2,"enabled":true}',
    b'{"version":1,"enabled":false}', b'{"version":1,"enabled":"true"}',
    b'{"version":1,"version":1,"enabled":true}',
    b'{"version":NaN,"enabled":true}', b" " * (egress.MAX_CONFIG + 1),
])
def test_public_policy_rejects_ambiguous_or_nonexact_schema(protected, raw):
    protected.policy.write_bytes(raw)
    with pytest.raises(egress.EgressError) as error:
        egress.native_environment()
    assert str(error.value) == egress.ERROR


@pytest.mark.parametrize("mode", [0o664, 0o666, 0o777])
def test_public_policy_rejects_writable_permissions(protected, mode):
    protected.policy.chmod(mode)
    with pytest.raises(egress.EgressError):
        egress.enabled()


def test_protected_files_reject_symlinks_hardlinks_and_wrong_owner(protected, monkeypatch):
    real = protected.directory / "real.json"
    protected.policy.rename(real)
    protected.policy.symlink_to(real)
    with pytest.raises(egress.EgressError):
        egress.enabled()
    protected.policy.unlink()
    os.link(real, protected.policy)
    with pytest.raises(egress.EgressError):
        egress.enabled()
    protected.policy.unlink()
    real.rename(protected.policy)
    original = os.fstat

    def different_owner(fd):
        fields = list(original(fd))
        fields[4] = os.getuid() + 1
        return os.stat_result(fields)

    with monkeypatch.context() as scoped:
        scoped.setattr(egress.os, "fstat", different_owner)
        with pytest.raises(egress.EgressError):
            egress.enabled()


def test_public_policy_rejects_symlink_or_writable_parent(protected, monkeypatch):
    alias = protected.directory.parent / "alias"
    alias.symlink_to(protected.directory, target_is_directory=True)
    monkeypatch.setattr(egress, "POLICY_FILE", alias / protected.policy.name)
    with pytest.raises(egress.EgressError):
        egress.enabled()
    monkeypatch.setattr(egress, "POLICY_FILE", protected.policy)
    protected.directory.chmod(0o777)
    with pytest.raises(egress.EgressError):
        egress.enabled()


def test_private_config_is_never_accepted_world_readable(protected):
    protected.upstream.chmod(0o644)
    with pytest.raises(egress.EgressError) as error:
        egress.upstream_config()
    assert PRIVATE not in str(error.value) and str(error.value) == egress.ERROR


def test_private_config_rejects_extra_fields(protected):
    write_json(protected.upstream, {**protected.config, "fallback": "direct"}, 0o600)
    with pytest.raises(egress.EgressError):
        egress.upstream_config()


@pytest.mark.parametrize("field,value", [
    ("version", True), ("version", 2), ("scheme", "socks4"), ("host", "bad host"),
    ("host", "proxy.example.test/path"), ("port", True), ("port", 0), ("port", 65536),
    ("username", ""), ("username", "user:name"), ("username", "user\r\nHeader: injected"),
    ("password", ""), ("password", "private\x00value"), ("password", "x" * 2049),
    ("allowed_hosts", []), ("allowed_hosts", [TARGET, TARGET]),
    ("allowed_hosts", ["127.0.0.1"]), ("allowed_hosts", ["::1"]),
    ("allowed_hosts", ["*.anthropic.com"]), ("allowed_hosts", ["https://" + TARGET]),
])
def test_private_config_validates_proxy_auth_and_explicit_target_set(protected, field, value):
    write_json(protected.upstream, {**protected.config, field: value}, 0o600)
    with pytest.raises(egress.EgressError) as error:
        egress.upstream_config()
    assert str(error.value) == egress.ERROR


def test_absent_policy_keeps_legacy_route_but_present_invalid_policy_does_not(protected):
    protected.policy.unlink()
    assert egress.native_environment() == {}
    protected.policy.symlink_to(protected.directory / "missing")
    with pytest.raises(egress.EgressError):
        egress.native_environment()


def test_native_exec_overwrites_hostile_routes_without_reading_upstream_credentials(
        protected, monkeypatch):
    route = egress.native_environment()
    for key in route:
        monkeypatch.setenv(key, "http://private-user:private-pass@untrusted.invalid")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "SLOT_OWN_AUTH_STAYS_LOCAL")
    monkeypatch.setattr(egress, "upstream_config", lambda: pytest.fail("read private config"))
    captured = {}
    monkeypatch.setattr(egress.os, "execvpe", lambda command, argv, environment:
                        captured.update(command=command, argv=argv, environment=environment))
    assert egress.main(["exec", "--", "/native/claude", "auth", "status"]) == 0
    assert captured["argv"] == ["/native/claude", "auth", "status"]
    environment = captured["environment"]
    assert all(environment[key] == value for key, value in route.items())
    assert environment["NO_PROXY"] == environment["ALL_PROXY"] == ""
    assert environment["ANTHROPIC_AUTH_TOKEN"] == "SLOT_OWN_AUTH_STAYS_LOCAL"
    assert PRIVATE not in json.dumps(environment)
    assert "private-user:private-pass" not in json.dumps(environment)


def test_native_exec_invalid_policy_refuses_launch_with_generic_error(protected, monkeypatch, capsys):
    protected.policy.write_text(PRIVATE)
    monkeypatch.setattr(egress.os, "execvpe", lambda *a: pytest.fail("launched native command"))
    assert egress.main(["exec", "--", "/native/claude"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == egress.ERROR + "\n"
    assert PRIVATE not in captured.err


@pytest.mark.parametrize("version", [b"1.0", b"1.1"])
def test_connect_accepts_only_the_exact_allowed_authority(version):
    lines = [b"CONNECT API.ANTHROPIC.COM:443 HTTP/" + version,
             b"Host: api.anthropic.com:443", b"Content-Length: 0"]
    assert egress._destination(lines, [TARGET]) == TARGET


@pytest.mark.parametrize("line,headers", [
    (b"CONNECT api.anthropic.com.evil.test:443 HTTP/1.1", []),
    (b"CONNECT evil.api.anthropic.com:443 HTTP/1.1", []),
    (b"CONNECT api.anthropic.com:80 HTTP/1.1", []),
    (b"CONNECT api.anthropic.com:444 HTTP/1.1", []),
    (b"CONNECT user@api.anthropic.com:443 HTTP/1.1", []),
    (b"CONNECT api.anthropic.com.:443 HTTP/1.1", []),
    (b"CONNECT 127.0.0.1:443 HTTP/1.1", []),
    (b"CONNECT [::1]:443 HTTP/1.1", []),
    (b"GET https://api.anthropic.com/v1/messages HTTP/1.1", []),
    (b"CONNECT api.anthropic.com:443 HTTP/2", []),
    (b"CONNECT api.anthropic.com:443 HTTP/1.1", [b"Proxy-Authorization: Basic private"]),
    (b"CONNECT api.anthropic.com:443 HTTP/1.1", [b"pRoXy-AuThOrIzAtIoN: private"]),
    (b"CONNECT api.anthropic.com:443 HTTP/1.1", [b"Authorization: private"]),
    (b"CONNECT api.anthropic.com:443 HTTP/1.1", [b"Transfer-Encoding: chunked"]),
    (b"CONNECT api.anthropic.com:443 HTTP/1.1", [b"Content-Length: 1"]),
    (b"CONNECT api.anthropic.com:443 HTTP/1.1", [b"Host: different.example:443"]),
    (b"CONNECT api.anthropic.com:443 HTTP/1.1", [b"Host: api.anthropic.com:443",
                                                 b"host: api.anthropic.com:443"]),
    (b"CONNECT api.anthropic.com:443 HTTP/1.1", [b"X-Invalid: private\x00value"]),
])
def test_untrusted_connect_requests_cannot_change_destination_or_auth(line, headers):
    with pytest.raises(egress.EgressError):
        egress._destination([line, *headers], [TARGET])


class Stream:
    def __init__(self, incoming=b""):
        self.incoming = bytearray(incoming)
        self.sent = bytearray()
        self.closed = False

    def recv(self, size):
        value = bytes(self.incoming[:size])
        del self.incoming[:size]
        return value

    def sendall(self, data):
        self.sent.extend(data)

    def settimeout(self, timeout):
        pass

    def close(self):
        self.closed = True


def test_connect_headers_are_bounded_and_leave_tunnel_bytes_untouched():
    stream = Stream(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\nopaque-tunnel-data")
    assert egress._headers(stream) == [b"CONNECT api.anthropic.com:443 HTTP/1.1"]
    assert stream.incoming == b"opaque-tunnel-data"
    for incoming in (b"x" * (egress.MAX_HEADERS + 1), b"x" * 4097 + b"\r\n\r\n",
                     b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n"):
        with pytest.raises(egress.EgressError):
            egress._headers(Stream(incoming))


def test_upstream_rejection_is_closed_and_redacted(protected, monkeypatch):
    stream = Stream(b"HTTP/1.1 407 " + PRIVATE.encode() + b"\r\nProxy-Authenticate: private\r\n\r\n")
    attempted = []
    monkeypatch.setattr(egress.socket, "create_connection", lambda address, timeout:
                        attempted.append(address) or stream)
    config = {**protected.config, "scheme": "http"}
    with pytest.raises(egress.EgressError) as error:
        egress._upstream(config, TARGET)
    assert str(error.value) == egress.ERROR and stream.closed
    assert attempted == [(config["host"], config["port"])]
    credential = base64.b64encode((config["username"] + ":" + PRIVATE).encode())
    assert b"Proxy-Authorization: Basic " + credential in stream.sent


def test_local_forwarder_never_reflects_proxy_errors_or_headers(protected, monkeypatch):
    client = Stream(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n")

    def rejected(config, target):
        raise OSError(PRIVATE)

    monkeypatch.setattr(egress, "_upstream", rejected)
    egress._Handler(client, ("127.0.0.1", 12345), object())
    assert bytes(client.sent) == egress._FAILURE
    assert PRIVATE.encode() not in client.sent


def test_proxy_unavailable_never_falls_back_to_provider_directly(protected, monkeypatch):
    attempted = []

    def unavailable(address, timeout):
        attempted.append(address)
        raise OSError(PRIVATE)

    monkeypatch.setattr(egress.socket, "create_connection", unavailable)
    context = ssl.create_default_context()
    connection = egress.provider_connection(TARGET, 443, timeout=1, context=context)
    with pytest.raises(egress.EgressError) as error:
        connection.connect()
    assert attempted == [("127.0.0.1", 912)]
    assert str(error.value) == egress.ERROR and PRIVATE not in str(error.value)


def test_invalid_policy_cannot_construct_a_direct_provider_connection(protected, monkeypatch):
    protected.policy.write_text("invalid private policy")
    monkeypatch.setattr(egress.http.client, "HTTPSConnection",
                        lambda *a, **kw: pytest.fail("constructed a direct connection"))
    with pytest.raises(egress.EgressError):
        egress.provider_connection(TARGET, 443, timeout=1, context=ssl.create_default_context())
