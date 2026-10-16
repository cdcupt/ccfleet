"""Machine diagnostics measure the execution route without rerouting management."""
from __future__ import annotations

import io
import ssl
from dataclasses import replace

import pytest

from ccfleet_agent import machine

UNKNOWN = {"ip": None, "source": None}


@pytest.fixture
def cfg(tmp_path):
    return machine.MachineConfig.from_env({
        "CCFLEET_URL": "https://fleet.example", "CCFLEET_NODE_ID": "machine-1",
        "CCFLEET_NODE_TOKEN": "a" * 64, "CCFLEET_LIB_DIR": str(tmp_path),
        "CCFLEET_EGRESS_TARGETS": "https://echo.example/ip"})


class Response(io.BytesIO):
    def __init__(self, body=b"198.51.100.9", status=200):
        super().__init__(body)
        self.status = status
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


@pytest.fixture
def proxy(monkeypatch):
    attempts = []
    outcomes = []

    class Connection:
        def __init__(self, host, port, *, timeout, context):
            self.host, self.port, self.timeout, self.context = host, port, timeout, context
            self.closed = False
            self.response = outcomes.pop(0) if outcomes else Response()
            self.requests = []
            attempts.append(self)

        def request(self, method, path, *, headers):
            self.requests.append((method, path, headers))
            if isinstance(self.response, BaseException):
                raise self.response

        def getresponse(self):
            return self.response

        def close(self):
            self.closed = True

    monkeypatch.setattr(machine.egress, "enabled", lambda: True)
    monkeypatch.setattr(machine.egress, "_ProxiedHTTPSConnection", Connection)
    return attempts, outcomes


def protected_system():
    def management(request, timeout=None):
        pytest.fail("proxy execution sample used the management opener")
    return machine.System(opener=management)


def test_direct_nodes_keep_the_existing_management_probe(cfg, monkeypatch, tmp_path):
    monkeypatch.setattr(machine.egress, "enabled", lambda: False)
    monkeypatch.setattr(machine.egress, "UPSTREAM_FILE", tmp_path / "missing-upstream.json")
    calls = []

    def direct(request, timeout=None):
        calls.append((request.full_url, timeout))
        return Response(b"192.0.2.2")

    system = machine.System(opener=direct)
    assert machine.execution_egress_ip(cfg, system) == {"ip": "192.0.2.2", "source": "echo.example"}
    assert calls == [("https://echo.example/ip", 5.0)] and system.opener is direct


def test_missing_public_policy_with_private_config_is_unknown(cfg, monkeypatch, tmp_path):
    private = tmp_path / "upstream.json"
    private.touch(mode=0o600)
    monkeypatch.setattr(machine.egress, "enabled", lambda: False)
    monkeypatch.setattr(machine.egress, "UPSTREAM_FILE", private)
    assert machine.execution_egress_ip(cfg, protected_system()) == UNKNOWN


@pytest.mark.parametrize("error", [PermissionError(), OSError("PRIVATE filesystem detail")])
def test_private_config_metadata_failure_never_uses_direct_route(cfg, monkeypatch, error, caplog):
    class Unreadable:
        def lstat(self):
            raise error

    monkeypatch.setattr(machine.egress, "enabled", lambda: False)
    monkeypatch.setattr(machine.egress, "UPSTREAM_FILE", Unreadable())
    assert machine.execution_egress_ip(cfg, protected_system()) == UNKNOWN
    assert not caplog.text


def test_proxy_sample_is_fresh_verified_tls_and_never_uses_vps_opener(cfg, proxy, monkeypatch):
    attempts, _ = proxy
    monkeypatch.setenv("HTTPS_PROXY", "http://wrong.example")
    monkeypatch.setenv("NO_PROXY", "*")
    system = protected_system()
    first = machine.execution_egress_ip(cfg, system)
    second = machine.execution_egress_ip(cfg, system)
    assert first == second == {"ip": "198.51.100.9", "source": "echo.example"}
    assert len(attempts) == 2 and attempts[0] is not attempts[1]
    for connection in attempts:
        assert (connection.host, connection.port, connection.timeout) == ("echo.example", 443, 5.0)
        assert connection.context.check_hostname
        assert connection.context.verify_mode == ssl.CERT_REQUIRED
        assert connection.requests[0][:2] == ("GET", "/ip")
        assert connection.requests[0][2]["Connection"] == "close"
        assert connection.closed and connection.response.closed
        assert connection.response.read_sizes == [machine.core.MAX_EGRESS_BYTES + 1]


@pytest.mark.parametrize("target", [
    "http://echo.example/ip", "ftp://echo.example/ip", "https://user:pass@echo.example/ip",
    "https://echo.example:444/ip", "https://echo.example:/ip", "https:///ip",
    "https://echo.example/ip#fragment", "https://echo.example/\r\nInjected: value",
    "not a URL", "https://[invalid/ip",
])
def test_malformed_or_non_https_reflectors_never_open_any_route(cfg, proxy, target):
    attempts, _ = proxy
    assert machine.execution_egress_ip(replace(cfg, egress_targets=(target,)),
                                       protected_system()) == UNKNOWN
    assert attempts == []


@pytest.mark.parametrize("outcome", [
    OSError("PRIVATE upstream detail"), ssl.SSLCertVerificationError("PRIVATE TLS detail"),
    Response(b"198.51.100.9", 302), Response(b"198.51.100.9", 500),
    Response(b"not an IP"), Response(b""), Response(b"x" * 100),
])
def test_proxy_tls_status_and_body_failures_are_unknown_and_closed(cfg, proxy, outcome, caplog):
    attempts, outcomes = proxy
    outcomes.append(outcome)
    assert machine.execution_egress_ip(cfg, protected_system()) == UNKNOWN
    assert attempts[0].closed
    if isinstance(outcome, Response):
        assert outcome.closed
    assert "PRIVATE" not in caplog.text


def test_bounded_proxy_only_fallback_and_timeout(cfg, proxy):
    attempts, outcomes = proxy
    outcomes.extend([OSError("not available")] * 3)
    cfg = replace(cfg, timeout_s=100, egress_targets=tuple(
        f"https://echo{number}.example/ip" for number in range(5)))
    assert machine.execution_egress_ip(cfg, protected_system()) == UNKNOWN
    assert len(attempts) == 3 and all(c.timeout == 5 for c in attempts)


def test_valid_ip_prefix_with_oversized_body_is_rejected(cfg, proxy):
    attempts, outcomes = proxy
    prefix = b"198.51.100.9"
    body = prefix + b" " * (machine.core.MAX_EGRESS_BYTES - len(prefix)) + b"TRAILING"
    outcomes.append(Response(body))
    assert machine.execution_egress_ip(cfg, protected_system()) == UNKNOWN
    assert attempts[0].closed and attempts[0].response.closed
    assert attempts[0].response.read_sizes == [machine.core.MAX_EGRESS_BYTES + 1]


def test_next_https_reflector_can_succeed_without_direct_fallback(cfg, proxy):
    attempts, outcomes = proxy
    outcomes.extend([OSError("failed"), Response()])
    cfg = replace(cfg, timeout_s=2, egress_targets=("https://one.example", "https://two.example"))
    assert machine.execution_egress_ip(cfg, protected_system())["source"] == "two.example"
    assert len(attempts) == 2 and all(c.timeout == 2 for c in attempts)


def test_invalid_policy_is_unknown_before_opening_any_route(cfg, proxy, monkeypatch, caplog):
    def invalid():
        raise machine.egress.EgressError()
    monkeypatch.setattr(machine.egress, "enabled", invalid)
    assert machine.execution_egress_ip(cfg, protected_system()) == UNKNOWN
    assert proxy[0] == [] and not caplog.text


def test_policy_removed_during_sample_invalidates_observation(cfg, proxy, monkeypatch):
    enabled = iter([True, False])
    monkeypatch.setattr(machine.egress, "enabled", lambda: next(enabled))
    assert machine.execution_egress_ip(cfg, protected_system()) == UNKNOWN
    assert proxy[0][0].closed


def test_payload_uses_execution_probe_and_preserves_heartbeat_opener(cfg, proxy):
    posted = []

    def management(request, timeout=None):
        posted.append(request)
        return Response(b'{"ok":true}')

    system = machine.System(opener=management)
    payload = machine.machine_payload(cfg, {}, system)
    assert payload["egress"] == {"ip": "198.51.100.9", "source": "echo.example"}
    assert not posted and system.opener is management
    status, _ = machine.core.send_heartbeat(cfg, payload, system.opener)
    assert status == 200 and len(posted) == 1
    assert posted[0].full_url == cfg.url + "/api/heartbeat"
