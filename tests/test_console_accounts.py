"""Per-user console accounts and the boundary around them.

An owner sees only their own nodes, and the only thing they may change is their
own sign-in — the one action that would otherwise force them back to SSH. The
point of these tests is that boundary, not the happy path: an owner must not see
another owner's node, must not take any other action on their own, and must not
take even a permitted action on somebody else's, including by hand-building the
request with a valid CSRF token.
"""
import base64
import http.client
import json
import re
import threading

import pytest

from ccfleetd.api import Context, build_server, csrf_token
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.passwords import hash_password
from ccfleetd.store import Store, StoreError
from tests.conftest import heartbeat


@pytest.fixture
def fleet(cfg):
    """Two owners, one node each, plus a login for one of them."""
    store = Store(":memory:")
    store.add_node("node-a", "alice", now=1.0)
    store.add_node("node-b", "bob", now=1.0)
    store.insert_heartbeat("node-a", 1.0, heartbeat(1.0)["payload"])
    store.insert_heartbeat("node-b", 1.0, heartbeat(1.0)["payload"])
    store.add_user("alice", hash_password("alice-pw"), "owner", "alice", 1.0)
    store.add_user("ops", hash_password("ops-pw"), "admin", now=1.0)
    ctx = Context(store, cfg, Monitor(store, cfg, LogNotifier()))
    srv = build_server(ctx, host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, store, cfg
    srv.shutdown()
    srv.server_close()
    store.close()


def call(srv, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, raw


def creds(user, password):
    blob = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": "Basic " + blob}


def test_the_admin_token_keeps_working_exactly_as_before(fleet):
    """Existing scripts and bookmarks must not break when accounts are added."""
    srv, _, cfg = fleet
    status, raw = call(srv, "GET", "/api/nodes", headers=creds("anyone", cfg.admin_token))
    assert status == 200
    assert {n["id"] for n in json.loads(raw)["nodes"]} == {"node-a", "node-b"}


def test_an_owner_sees_only_their_own_node(fleet):
    srv, _, _ = fleet
    status, raw = call(srv, "GET", "/api/nodes", headers=creds("alice", "alice-pw"))
    assert status == 200
    assert [n["id"] for n in json.loads(raw)["nodes"]] == ["node-a"], \
        "bob's node must not be visible to alice"


def test_an_owners_dashboard_does_not_mention_the_other_node(fleet):
    """Filtering the API but leaking the name into the HTML would still be a leak."""
    srv, _, _ = fleet
    status, raw = call(srv, "GET", "/", headers=creds("alice", "alice-pw"))
    assert status == 200
    page = raw.decode()
    assert "node-a" in page and "node-b" not in page


def test_an_owner_gets_their_own_credentials_and_nothing_else(fleet):
    """An owner may do the two things that are about their own access.

    Signing their node in, and minting a token for their own machines. Both
    are credentials for the account they already own, and routing either
    through the operator would only move the bottleneck. Everything that
    manages the fleet stays with the operator.
    """
    srv, _, _ = fleet
    _, raw = call(srv, "GET", "/", headers=creds("alice", "alice-pw"))
    page = raw.decode()
    assert "Add a node" not in page and "Manage nodes" not in page
    actions = set(re.findall(r'action="/actions/node/([^/]+)/([^"]+)"', page))
    assert actions, "an owner should be able to start their own sign-in"
    assert {a for _, a in actions} <= {"login-start", "login-code", "login-cancel",
                                       "token-start", "token-show"}
    assert "token-start" in {a for _, a in actions}, "their own device token"
    # None of the fleet-management actions are offered, whatever else is.
    assert not {a for _, a in actions} & {"disable", "enable", "pin", "rc-on", "rc-off",
                                          "rotate-token", "remove"}
    assert {n for n, _ in actions} == {"node-a"}, "only their own node"


def test_an_owner_cannot_act_even_by_hand_building_the_request(fleet):
    """The missing forms are cosmetic; this is the control that actually holds."""
    srv, store, cfg = fleet
    body = f"csrf={csrf_token(cfg)}".encode()
    headers = {**creds("alice", "alice-pw"),
               "Content-Type": "application/x-www-form-urlencoded",
               "Content-Length": str(len(body))}
    status, _ = call(srv, "POST", "/actions/node/node-a/disable", body, headers)
    assert status == 403, "an owner's credentials are valid, the action is not theirs"
    assert store.get_node("node-a")["enabled"], "the node must be untouched"


def test_an_owner_cannot_act_on_someone_elses_node_either(fleet):
    srv, store, cfg = fleet
    body = f"csrf={csrf_token(cfg)}".encode()
    headers = {**creds("alice", "alice-pw"),
               "Content-Type": "application/x-www-form-urlencoded",
               "Content-Length": str(len(body))}
    assert call(srv, "POST", "/actions/node/node-b/disable", body, headers)[0] == 403
    assert store.get_node("node-b")["enabled"]


def test_an_admin_account_manages_the_fleet_like_the_token_does(fleet):
    srv, store, cfg = fleet
    body = f"csrf={csrf_token(cfg)}".encode()
    headers = {**creds("ops", "ops-pw"),
               "Content-Type": "application/x-www-form-urlencoded",
               "Content-Length": str(len(body))}
    status, _ = call(srv, "POST", "/actions/node/node-b/disable", body, headers)
    assert status in (200, 303)
    assert not store.get_node("node-b")["enabled"]


def test_bad_credentials_are_401_and_reveal_nothing_about_which_part_was_wrong(fleet):
    srv, _, _ = fleet
    for user, password in (("alice", "wrong"), ("nobody", "alice-pw"), ("", ""),
                           ("alice", "")):
        status, raw = call(srv, "GET", "/api/nodes", headers=creds(user, password))
        assert status == 401
        assert b"alice" not in raw and b"nobody" not in raw


def test_alerts_are_scoped_too(fleet):
    srv, store, _ = fleet
    store.open_alert("node-b", "disk_high", "warn", "disk 90% used", 1.0)
    _, raw = call(srv, "GET", "/api/alerts", headers=creds("alice", "alice-pw"))
    assert json.loads(raw)["alerts"] == [], "bob's alert must not reach alice"
    _, raw = call(srv, "GET", "/api/alerts", headers=creds("ops", "ops-pw"))
    assert len(json.loads(raw)["alerts"]) == 1


def test_a_user_cannot_shadow_the_operator_by_picking_a_name(fleet):
    """The admin token is checked first, so any name plus the token is admin."""
    srv, store, cfg = fleet
    store.add_user("admin", hash_password("not-the-token"), "owner", "alice", 1.0)
    _, raw = call(srv, "GET", "/api/nodes", headers=creds("admin", cfg.admin_token))
    assert len(json.loads(raw)["nodes"]) == 2, "the token still wins"
    _, raw = call(srv, "GET", "/api/nodes", headers=creds("admin", "not-the-token"))
    assert len(json.loads(raw)["nodes"]) == 1, "and the account is still only an owner"


def test_the_store_refuses_nonsense_accounts():
    store = Store(":memory:")
    for bad in ("", "Alice", "_alice", "alice!", "a" * 33):
        with pytest.raises(StoreError):
            store.add_user(bad, hash_password("x"), "owner", now=1.0)
    with pytest.raises(StoreError):
        store.add_user("alice", hash_password("x"), "superuser", now=1.0)
    with pytest.raises(StoreError):
        store.add_user("alice", "", "owner", now=1.0)
    store.add_user("alice", hash_password("x"), "owner", now=1.0)
    with pytest.raises(StoreError):
        store.add_user("alice", hash_password("y"), "owner", now=1.0)
    store.close()


def test_listing_accounts_never_exposes_a_hash():
    store = Store(":memory:")
    store.add_user("alice", hash_password("secret"), "owner", now=1.0)
    blob = json.dumps(store.list_users())
    assert "pbkdf2" not in blob and "secret" not in blob
    store.close()


def test_an_owner_may_start_a_sign_in_on_their_own_node(fleet):
    srv, store, cfg = fleet
    body = f"csrf={csrf_token(cfg)}&email=alice%40example.com".encode()
    headers = {**creds("alice", "alice-pw"),
               "Content-Type": "application/x-www-form-urlencoded",
               "Content-Length": str(len(body))}
    status, _ = call(srv, "POST", "/actions/node/node-a/login-start", body, headers)
    assert status in (200, 303), status
    login = store.get_login("node-a")
    assert login["state"] == "requested" and login["email"] == "alice@example.com"


def test_an_owner_may_not_start_a_sign_in_on_someone_elses_node(fleet):
    """A permitted action is still only permitted on a node they own."""
    srv, store, cfg = fleet
    body = f"csrf={csrf_token(cfg)}".encode()
    headers = {**creds("alice", "alice-pw"),
               "Content-Type": "application/x-www-form-urlencoded",
               "Content-Length": str(len(body))}
    status, _ = call(srv, "POST", "/actions/node/node-b/login-start", body, headers)
    assert status == 403
    assert store.get_login("node-b") is None


def test_a_csrf_token_is_not_authorisation(fleet):
    """An owner now holds a valid CSRF token; it must buy them nothing extra."""
    srv, store, cfg = fleet
    body = f"csrf={csrf_token(cfg)}".encode()
    headers = {**creds("alice", "alice-pw"),
               "Content-Type": "application/x-www-form-urlencoded",
               "Content-Length": str(len(body))}
    for action in ("disable", "rc-off", "rotate-token", "remove"):
        status, _ = call(srv, "POST", f"/actions/node/node-a/{action}", body, headers)
        assert status == 403, f"{action} must stay with the operator"
    assert store.get_node("node-a") is not None and store.get_node("node-a")["enabled"]
