"""Configured-origin migration, using local HTTP and fake gateway/OAuth only."""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.parse
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ccfleetd import oauth, sessions
from ccfleetd.api import Context, build_server
from ccfleetd.config import Config, ConfigError
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from tests.conftest import heartbeat
from tests.test_admin_host import Browser
from tests.test_cli_access import active_slot, public_key

NEW = "https://ccfleet.io"
OLD = "https://ccfleet.daichenlab.com"
ADMIN = "console.example.com"


def config(**extra):
    return Config.from_env({"CCFLEET_PUBLIC_URL": NEW,
        "CCFLEET_PUBLIC_URL_ALIASES": OLD, "CCFLEET_ADMIN_HOST": ADMIN,
        "CCFLEET_ADMIN_TOKEN": "synthetic-admin-token-long-enough",
        "CCFLEET_GOOGLE_CLIENT_ID": "synthetic-client", "CCFLEET_GOOGLE_CLIENT_SECRET": "synthetic",
        "CCFLEET_COOKIE_SECRET": "synthetic-cookie-secret-long-enough",
        "CCFLEET_COOKIE_SECURE": "false", "CCFLEET_GATEWAY_ENABLED": "true",
        "CCFLEET_GATEWAY_SSH_KEY_PATH": "/unused/fake-key",
        "CCFLEET_GATEWAY_SSH_PUBLIC_KEY": public_key(9), **extra})


@pytest.mark.parametrize("host,expected", [("ccfleet.io", NEW), ("CCFLEET.IO:443", NEW),
    ("ccfleet.daichenlab.com", OLD), ("ccfleet.daichenlab.com:443", OLD),
    ("other.example.com", ""), ("ccfleet.io.evil.test", ""), ("ccfleet.io:80", ""),
    ("ccfleet.io:0", ""), ("ccfleet.io:", ""), ("ccfleet.io:65536", ""),
    ("user@ccfleet.io", ""), ("ccfleet.io/path", ""), ("ccfleet.io?x=y", ""),
    ("ccfleet.io#fragment", ""), (" ccfleet.io", ""), ("ccfleet.io\r\n", ""),
    (None, "")])
def test_only_configured_authorities_can_select_a_credential_origin(host, expected):
    assert config().public_origin_for_host(host) == expected


@pytest.mark.parametrize("aliases", ["https://old.example.com/path", "http://old.example.com",
    "https://u:p@old.example.com", "https://old.example.com?token=x",
    "https://old.example.com#fragment", "https://old.example.com:0", "https://old.example.com,",
    "https://old.example.com,https://OLD.example.com:443", NEW, "https://" + ADMIN,
    ",".join("https://alias" + str(i) + ".example.com" for i in range(9))])
def test_bad_or_ambiguous_alias_configuration_rejects_at_startup(aliases):
    with pytest.raises(ConfigError):
        config(CCFLEET_PUBLIC_URL_ALIASES=aliases)


def test_aliases_are_explicit_and_normalized_without_changing_the_canonical_url():
    cfg = config(CCFLEET_PUBLIC_URL_ALIASES=" https://OLD.example.com:443/ ")
    assert cfg.public_url == NEW and cfg.public_url_aliases == ("https://old.example.com",)
    assert cfg.public_origin_for_host("old.example.com") == "https://old.example.com"
    legacy = Config.from_env({"CCFLEET_PUBLIC_URL": "https://fleet.example.com"})
    assert legacy.public_url_aliases == ()
    assert legacy.public_origin_for_host("localhost:8110") == legacy.public_url


@pytest.fixture
def migration(store, monkeypatch):
    active_slot(store)
    cfg = config()
    exchanges = []

    def exchange(**kwargs):
        exchanges.append(kwargs["redirect_uri"])
        return "synthetic-access-token"

    monkeypatch.setattr(oauth, "exchange_code", exchange)
    monkeypatch.setattr(oauth, "fetch_identity", lambda *args, **kwargs:
                        {"sub": "sub-1", "email": "a@example.com"})
    ctx = Context(store, cfg, Monitor(store, cfg, LogNotifier()))
    ctx.gateway.close()

    class FakeGateway:
        enabled = True

        def status(self, key, pinned=""):
            return {"ready": True, "mode": key["mode"], "base_url": NEW,
                    "slots": [{"id": "s1", "name": "Owned", "state": "active", "ready": True}],
                    "key": key}

        def handle(self, handler):
            handler._json(200, {"fake_gateway": True, "provider_requests": 0})

    ctx.gateway = FakeGateway()
    server = build_server(ctx, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(method, path, *, origin=OLD, token=None, body=None, headers=None):
        supplied = {"Host": urllib.parse.urlsplit(origin).netloc, **(headers or {})}
        if token:
            supplied["Authorization"] = "Bearer " + token
        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
        connection.request(method, path, body=json.dumps(body) if body is not None else None,
                           headers=supplied)
        reply = connection.getresponse()
        raw = reply.read()
        result = reply.status, dict(reply.headers), raw
        connection.close()
        return result

    def paired(origin):
        pair = store.request_cli_pairing("s1", "a1", now=time.time())
        code, headers, raw = call("POST", "/api/cli/register", origin=origin, token=pair,
                                 body={"public_key": public_key(1), "device_name": "Synthetic"})
        assert code == 201 and "Location" not in headers
        return json.loads(raw)

    yield SimpleNamespace(store=store, cfg=cfg, ctx=ctx, server=server, call=call, paired=paired,
                          exchanges=exchanges)
    server.shutdown()
    server.server_close()
    thread.join(3)


@pytest.mark.parametrize("origin", [OLD, NEW])
def test_device_and_gateway_metadata_stay_on_the_origin_selected_by_the_client(migration, origin):
    m = migration
    device = m.paired(origin)
    assert device["endpoint"] == origin.replace("https:", "wss:") + "/api/cli/connect"
    assert device["gateway_base_url"] == origin
    for path in ("/api/cli/status", "/api/cli/gateway/options"):
        code, headers, raw = m.call("GET", path, origin=origin, token=device["device_token"])
        assert code == 200 and "Location" not in headers
        answer = json.loads(raw)
        assert answer.get("gateway_base_url", answer.get("base_url")) == origin
    code, _, raw = m.call("POST", "/api/cli/gateway", origin=origin, token=device["device_token"],
                         body={"slot_ids": ["s1"]})
    assert code == 201
    key = json.loads(raw)
    assert key["base_url"] == origin
    code, _, raw = m.call("GET", "/api/gateway/status", origin=origin, token=key["secret"])
    assert code == 200 and json.loads(raw)["base_url"] == origin
    assert m.store.resolve_gateway_key(key["secret"], now=time.time())["id"] == key["key"]["id"]
    assert m.call("GET", "/api/cli/connect", origin=origin, token=device["device_token"])[0] == 426
    code, _, raw = m.call("POST", "/v1/messages", origin=origin, token=key["secret"], body={})
    assert code == 200 and json.loads(raw)["provider_requests"] == 0


def test_existing_key_and_device_keep_working_without_automatic_repair_or_new_tokens(migration):
    m = migration
    device = m.store.register_cli_device(m.store.request_cli_pairing("s1", "a1", now=time.time()),
        public_key(2), "Already paired", now=time.time())
    made = m.store.create_gateway_key("a1", "Existing endpoint", ["s1"], now=time.time())
    before = m.store.resolve_gateway_key(made["key_token"], now=time.time())
    for origin in (OLD, NEW):
        code, headers, raw = m.call("GET", "/api/gateway/status", origin=origin,
                                    token=made["key_token"])
        assert code == 200 and json.loads(raw)["base_url"] == origin and "Location" not in headers
        assert m.call("GET", "/api/cli/status", origin=origin, token=device["device_token"])[0] == 200
    after = m.store.resolve_gateway_key(made["key_token"], now=time.time())
    assert (before["id"], before["generation"]) == (after["id"], after["generation"])


def test_host_poisoning_and_forwarded_headers_never_retarget_credentials(migration):
    m = migration
    device = m.paired(OLD)
    for host in ("attacker.example.com", "ccfleet.io:0", "ccfleet.io.evil.test"):
        assert m.call("GET", "/api/cli/gateway/options", token=device["device_token"],
            headers={"Host": host, "X-Forwarded-Host": "ccfleet.io"})[0] == 404
    code, _, raw = m.call("GET", "/api/cli/gateway/options", token=device["device_token"],
        headers={"X-Forwarded-Host": "attacker.example.com", "X-Forwarded-Proto": "http"})
    assert code == 200 and json.loads(raw)["base_url"] == OLD


def test_duplicate_host_is_rejected_instead_of_selecting_one_header(migration):
    conn = http.client.HTTPConnection(*migration.server.server_address, timeout=3)
    conn.putrequest("GET", "/healthz", skip_host=True)
    conn.putheader("Host", "ccfleet.io")
    conn.putheader("Host", "attacker.example.com")
    conn.endheaders()
    reply = conn.getresponse()
    assert reply.status == 404
    reply.read()
    conn.close()


@pytest.mark.parametrize("origin", [OLD, NEW])
def test_heartbeat_keeps_its_old_post_origin_and_does_not_redirect(migration, origin):
    m = migration
    token = m.store.rotate_token("m1")
    code, headers, _ = m.call("POST", "/api/heartbeat", origin=origin, token=token,
                             body={**heartbeat(time.time())["payload"], "node_id": "m1"})
    assert code == 200 and "Location" not in headers


def test_oauth_stays_canonical_admin_callback_and_host_only_cookies_are_preserved(migration):
    m = migration
    for host, expected in (("ccfleet.daichenlab.com", OLD + "/auth/google/callback"),
                           ("ccfleet.io", NEW + "/auth/google/callback"),
                           (ADMIN, "https://" + ADMIN + "/auth/google/callback")):
        browser = Browser(m.server.server_address[1], host)
        assert browser.sign_in_with_google() == expected
        assert browser.jar
    assert m.call("GET", "/healthz", headers={"Host": "127.0.0.1:8110"})[0] == 200
    assert m.call("GET", "/api/nodes", origin=NEW)[0] == 404


@pytest.mark.parametrize("origin", [OLD, NEW])
def test_each_default_product_callback_requires_its_own_host_cookie_and_exchange_uri(migration, origin):
    m = migration
    browser = Browser(m.server.server_address[1], urllib.parse.urlsplit(origin).netloc)
    start = browser.call("GET", "/auth/google/start?next=/account")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(start.getheader("Location")).query)
    expected = origin + "/auth/google/callback"
    assert query["redirect_uri"] == [expected]
    assert sessions.FLOW_COOKIE_NAME in browser.jar
    assert all("domain=" not in value.lower() for value in start.headers.get_all("Set-Cookie"))
    callback = "/auth/google/callback?" + urllib.parse.urlencode({"state": query["state"][0], "code": "c"})
    opposite = Browser(m.server.server_address[1], urllib.parse.urlsplit(NEW if origin == OLD else OLD).netloc)
    assert opposite.call("GET", callback).status == 400
    assert sessions.COOKIE_NAME not in opposite.jar and m.exchanges == []
    back = browser.call("GET", callback)
    assert back.status == 303 and m.exchanges == [expected]
    assert sessions.COOKIE_NAME in browser.jar and sessions.FLOW_COOKIE_NAME not in browser.jar
    assert all("domain=" not in value.lower() for value in back.headers.get_all("Set-Cookie"))


def test_explicit_oauth_override_stays_exact_host_scoped_and_does_not_change_admin(migration):
    m = migration
    m.ctx.cfg = replace(m.cfg, google_redirect_uri=OLD + "/auth/google/callback")
    old = Browser(m.server.server_address[1], urllib.parse.urlsplit(OLD).netloc)
    assert old.sign_in_with_google() == OLD + "/auth/google/callback"
    code, headers, _ = m.call("GET", "/auth/google/start", origin=NEW)
    assert code == 404 and "Location" not in headers and "Set-Cookie" not in headers
    admin = Browser(m.server.server_address[1], ADMIN)
    assert admin.sign_in_with_google() == "https://" + ADMIN + "/auth/google/callback"
