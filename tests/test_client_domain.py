"""Canonical service requests preserve old saved pairing and gateway identity."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import runpy
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OLD = "https://ccfleet.daichenlab.com"
NEW = "https://ccfleet.io"
SECRET = "ccf_api_" + "x" * 43


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setenv("CCFLEET_HOME", str(tmp_path / "control"))
    monkeypatch.delenv("CCFLEET_SERVER", raising=False)
    return runpy.run_path(str(ROOT / "laptop/ccfleet"))


def test_import_does_not_load_personal_configuration_or_contact_either_origin(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("import contacted a transport")
    monkeypatch.setenv("CCFLEET_HOME", str(tmp_path / "absent-control"))
    monkeypatch.setattr(urllib.request, "build_opener", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    imported = runpy.run_path(str(ROOT / "laptop/ccfleet"))
    assert imported["DEFAULT_SERVER"] == NEW
    assert not (tmp_path / "absent-control").exists()


@pytest.mark.parametrize("old,new", [
    (OLD, NEW), (OLD + "/", NEW + "/"), (OLD + ":443/", NEW + "/"),
    (OLD + "/api/cli/status?slot=A%2FB&mode=one", NEW + "/api/cli/status?slot=A%2FB&mode=one"),
    (OLD + "/api/cli/status?", NEW + "/api/cli/status?"),
    ("wss://ccfleet.daichenlab.com/api/cli/connect", "wss://ccfleet.io/api/cli/connect"),
    ("wss://ccfleet.daichenlab.com:443/api/cli/connect?x=%2f", "wss://ccfleet.io/api/cli/connect?x=%2f"),
])
def test_only_literal_approved_aliases_resolve_with_exact_path_and_query(cli, old, new):
    assert cli["canonical_runtime_url"](old) == new
    assert cli["canonical_runtime_url"](new) == new


@pytest.mark.parametrize("url", [
    NEW, "https://fleet.example.test/api/cli/status", "wss://fleet.example.test/connect",
    "http://127.0.0.1:8123/v1/messages", "ws://localhost:8123/connect",
    "https://ccfleet.daichenlab.com.evil.test/api", "https://sub.ccfleet.daichenlab.com/api",
    "https://ccfleet.daichenlab.com:8443/api", "https://ccfleet.daichenlab.com:0443/api",
    "https://ccfleet.daichenlab.com./api", "https://CCFLEET.DAICHENLAB.COM/api",
    "HTTPS://ccfleet.daichenlab.com/api", "http://ccfleet.daichenlab.com/api",
    "https://%63cfleet.daichenlab.com/api",
])
def test_custom_loopback_nonstandard_or_lookalike_origins_are_not_retargeted(cli, url):
    assert cli["canonical_runtime_url"](url) == url


@pytest.mark.parametrize("url", [
    "https://user@ccfleet.daichenlab.com/api", "https://ccfleet.daichenlab.com@evil.test/api",
    "https://user:password@ccfleet.daichenlab.com/api", OLD + "/api#fragment",
    " https://ccfleet.daichenlab.com/api", "https://ccfleet.daichenlab.com\n.evil.test/api",
    OLD + "/api?x=\r\nsecret", "https://ccfleet.daichenlab.com\\@evil.test/api",
    OLD + ":99999/api", OLD + ":not-a-port/api", "https://[invalid/api", "",
])
def test_ambiguous_endpoints_reject_before_any_transport(cli, monkeypatch, url):
    def forbidden(*_args, **_kwargs):
        pytest.fail("unsafe endpoint reached a transport")
    monkeypatch.setattr(cli["urllib"].request, "build_opener", forbidden)
    monkeypatch.setattr(cli["socket"], "create_connection", forbidden)
    for function in (cli["api_json"], cli["open_websocket"]):
        with pytest.raises(cli["CliError"], match="endpoint URL is invalid"):
            function(url, "SYNTHETIC_PRIVATE_TOKEN")


def opener(cli, monkeypatch, response):
    calls = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, maximum):
            assert maximum == 256 * 1024
            return json.dumps(response).encode()

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            return Reply()

    def build(*handlers):
        assert len(handlers) == 1 and isinstance(handlers[0], cli["_NoRedirect"])
        return Opener()
    monkeypatch.setattr(cli["urllib"].request, "build_opener", build)
    return calls


def test_api_sends_same_token_and_payload_only_to_canonical_origin(cli, monkeypatch):
    calls = opener(cli, monkeypatch, {"authenticated": True})
    assert cli["api_json"](OLD + "/api/cli/status?x=%2F", "SYNTHETIC_TOKEN",
                           {"unchanged": "body"}) == {"authenticated": True}
    assert len(calls) == 1
    assert calls[0].full_url == NEW + "/api/cli/status?x=%2F"
    assert calls[0].get_header("Authorization") == "Bearer SYNTHETIC_TOKEN"
    assert json.loads(calls[0].data) == {"unchanged": "body"}


@pytest.mark.parametrize("failure", ["network", "redirect"])
def test_canonical_failure_never_retries_legacy_or_follows_redirect(cli, monkeypatch, failure):
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            if failure == "network":
                raise urllib.error.URLError("synthetic connection failure")
            raise urllib.error.HTTPError(request.full_url, 308, "redirect", {},
                                         io.BytesIO(b'{"error":"redirect refused"}'))
    monkeypatch.setattr(cli["urllib"].request, "build_opener", lambda *_a: Opener())
    with pytest.raises(cli["CliError"]):
        cli["api_json"](OLD + "/api/cli/status", "SYNTHETIC_TOKEN", method="GET")
    assert calls == [NEW + "/api/cli/status"]
    assert cli["_NoRedirect"]().redirect_request(None, None, 308, None, None, OLD) is None


def test_websocket_uses_canonical_dns_tls_host_and_same_path_token(cli, monkeypatch):
    connections, tls, writes = [], [], []

    class Socket:
        def settimeout(self, value):
            pass

        def sendall(self, raw):
            writes.append(raw)

        def recv(self, size):
            key = next(line.split(": ", 1)[1] for line in writes[-1].decode().split("\r\n")
                       if line.startswith("Sec-WebSocket-Key: "))
            accepted = base64.b64encode(hashlib.sha1(key.encode() + cli["WS_GUID"]).digest())
            return (b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                    b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accepted
                    + b"\r\n\r\nSYNTHETIC_INITIAL_BYTES")

        def close(self):
            pass

    sock = Socket()

    def connect(address, timeout):
        connections.append(address)
        return sock

    class Context:
        def wrap_socket(self, raw, server_hostname):
            tls.append(server_hostname)
            return raw
    monkeypatch.setattr(cli["socket"], "create_connection", connect)
    monkeypatch.setattr(cli["ssl"], "create_default_context", Context)
    actual, initial = cli["open_websocket"](
        "wss://ccfleet.daichenlab.com:443/api/cli/connect?x=%2F", "SYNTHETIC_DEVICE_TOKEN")
    assert actual is sock and initial == b"SYNTHETIC_INITIAL_BYTES"
    assert connections == [("ccfleet.io", 443)] and tls == ["ccfleet.io"]
    assert b"GET /api/cli/connect?x=%2F HTTP/1.1\r\nHost: ccfleet.io\r\n" in writes[0]
    assert b"Authorization: Bearer SYNTHETIC_DEVICE_TOKEN\r\n" in writes[0]
    assert b"ccfleet.daichenlab.com" not in writes[0]


def test_websocket_failure_does_not_retry_legacy(cli, monkeypatch):
    calls = []

    def fail(address, timeout):
        calls.append(address)
        raise OSError("synthetic handshake failure")
    monkeypatch.setattr(cli["socket"], "create_connection", fail)
    with pytest.raises(cli["CliError"]):
        cli["open_websocket"]("wss://ccfleet.daichenlab.com/api/cli/connect", "SYNTHETIC")
    assert calls == [("ccfleet.io", 443)]


def test_legacy_storage_and_key_identity_unchanged_after_loading_and_read_only_checks(cli, monkeypatch):
    cli["ensure_home"]()
    device = {"device_id": "same-device", "slot_id": "same-slot", "slot_name": "same-name",
              "device_token": "SYNTHETIC_DEVICE_TOKEN", "server": OLD,
              "endpoint": "wss://ccfleet.daichenlab.com/api/cli/connect",
              "key": "/private/key", "known_hosts": "/private/pin", "host_alias": "same-host"}
    config = {"version": 1, "active": device["device_id"],
              "devices": {device["device_id"]: device}, "projects": {"untouched": {"id": "record"}}}
    profile = {"version": 1, "base_url": OLD, "device_id": device["device_id"],
               "key_id": "same-key", "secret": SECRET, "slot_ids": ["same-slot"],
               "mode": "sticky", "provider": "devin", "generation": 8}
    paths = {cli["config_path"](): json.dumps(config, indent=3).encode() + b"\n",
             cli["home"]() / "gateway.json": json.dumps(profile, indent=4).encode() + b"\n"}
    for path, raw in paths.items():
        path.write_bytes(raw)
        path.chmod(0o600)
    # Merely loading cannot do I/O on either domain or transform identity guards.
    monkeypatch.setattr(cli["urllib"].request, "build_opener", lambda *_a: pytest.fail("load sent data"))
    raw_config = cli["load_config"]()
    selected = cli["choose_device"](raw_config)
    assert raw_config == config and selected == device
    loaded = cli["gateway_for_launch"](raw_config, selected)
    assert loaded == {**profile, "base_url": NEW}
    calls = opener(cli, monkeypatch, {"ready": True, "key": {"id": "same-key",
        "slot_ids": ["same-slot"], "mode": "sticky", "provider": "devin", "generation": 8}})
    cli["check_gateway"](loaded)
    cli["device_context"](selected)
    assert [request.full_url for request in calls] == [NEW + "/api/gateway/status", NEW + "/api/cli/status"]
    assert loaded == {**profile, "base_url": NEW} and selected == device
    assert all(path.read_bytes() == raw for path, raw in paths.items())


def test_unrelated_config_save_does_not_persist_runtime_canonicalization(cli):
    config = {"active": "device", "devices": {"device": {"device_id": "device", "slot_id": "slot",
              "server": OLD, "endpoint": "wss://ccfleet.daichenlab.com/api/cli/connect"}}}
    cli["save_config"](config)
    loaded = cli["load_config"]()
    selected = cli["choose_device"](loaded)
    assert selected == config["devices"]["device"]
    loaded["unrelated_preference"] = True
    cli["save_config"](loaded)
    assert cli["load_config"]()["devices"] == config["devices"]


@pytest.mark.parametrize("bare", [False, True])
def test_native_model_environment_gets_canonical_base_and_same_auth(cli, bare):
    env = {"USER_TOOL_ENV": "kept"}
    settings = cli["native_settings"](env, OLD, SECRET, bare=bare)
    assert env["ANTHROPIC_BASE_URL"] == settings["env"]["ANTHROPIC_BASE_URL"] == NEW
    assert env["ANTHROPIC_API_KEY" if bare else "ANTHROPIC_AUTH_TOKEN"] == SECRET
    assert env["USER_TOOL_ENV"] == "kept"


def test_fresh_defaults_and_version_are_new_without_loading_or_network(cli):
    assert cli["DEFAULT_SERVER"] == NEW and cli["CLIENT_VERSION"] == "0.2.6"
    assert cli["parser"]().parse_args(["login"]).server == NEW
    assert cli["parser"]().parse_args(["setup"]).server == NEW
    assert not cli["config_path"]().exists()
