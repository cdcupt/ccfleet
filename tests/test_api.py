import base64
import http.client
import json
import re
import threading
import time

import pytest

from ccfleetd.api import Context, build_server
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.store import Store
from tests.conftest import heartbeat


@pytest.fixture
def server(cfg):
    store = Store(":memory:")
    ctx = Context(store, cfg, Monitor(store, cfg, LogNotifier()))
    srv = build_server(ctx, host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, store
    srv.shutdown()
    srv.server_close()
    store.close()


def call(srv, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    conn.request(method, path, body=data, headers=headers or {})
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, raw, resp.getheader("Content-Type", "")


def basic(token):
    return {"Authorization": "Basic " + base64.b64encode(f"admin:{token}".encode()).decode()}


def test_healthz_and_unknown_paths(server):
    srv, _ = server
    assert call(srv, "GET", "/healthz")[0] == 200
    assert call(srv, "HEAD", "/healthz")[0] == 200
    assert call(srv, "GET", "/nope")[0] == 404
    assert call(srv, "POST", "/nope", {})[0] == 404


def test_admin_endpoints_require_auth(server, cfg):
    srv, _ = server
    assert call(srv, "GET", "/")[0] == 401
    assert call(srv, "GET", "/api/nodes", headers=basic("wrong"))[0] == 401
    assert call(srv, "GET", "/api/nodes", headers={"Authorization": "Basic !!!"})[0] == 401
    status, body, ctype = call(srv, "GET", "/", headers=basic(cfg.admin_token))
    assert status == 200 and ctype.startswith("text/html") and b"ccfleet" in body
    status, body, _ = call(srv, "GET", "/api/alerts",
                           headers={"Authorization": f"Bearer {cfg.admin_token}"})
    assert status == 200 and json.loads(body) == {"alerts": [], "recent": []}


def test_heartbeat_auth_and_validation(server):
    srv, store = server
    token = store.add_node("node-a", "erik", pinned_version="2.1.92")
    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    assert call(srv, "POST", "/api/heartbeat", {"node_id": "node-a"})[0] == 401
    assert call(srv, "POST", "/api/heartbeat", {"node_id": "node-a"},
                {"Authorization": "Bearer nothex"})[0] == 401
    assert call(srv, "POST", "/api/heartbeat", b"{not json", auth)[0] == 400
    assert call(srv, "POST", "/api/heartbeat", {"node_id": "node-b"}, auth)[0] == 400
    assert call(srv, "POST", "/api/heartbeat", b"x" * 70000, auth)[0] == 413


def test_heartbeat_round_trip(server, cfg):
    srv, store = server
    token = store.add_node("node-a", "erik", pinned_version="2.1.92")
    auth = {"Authorization": f"Bearer {token}"}
    payload = heartbeat(time.time())["payload"]
    status, body, _ = call(srv, "POST", "/api/heartbeat", payload, auth)
    assert status == 200
    reply = json.loads(body)
    assert reply["ok"] is True and reply["pinned_version"] == "2.1.92"
    assert reply["open_alerts"] == []
    stored = store.recent_heartbeats("node-a")[0]["payload"]
    assert stored["egress"]["ip"] == "203.0.113.10" and "accessToken" not in json.dumps(stored)
    status, body, _ = call(srv, "GET", "/api/nodes", headers=basic(cfg.admin_token))
    rows = json.loads(body)["nodes"]
    assert rows[0]["id"] == "node-a" and rows[0]["egress_ip"] == "203.0.113.10"
    assert rows[0]["status"] == "ok"
    bad_disk = {**payload, "disk": {"used_pct": 99.0}}
    reply = json.loads(call(srv, "POST", "/api/heartbeat", bad_disk, auth)[1])
    assert reply["open_alerts"] == ["disk_high"] and reply["events"] == 1


def csrf_for(cfg):
    from ccfleetd.api import csrf_token
    return csrf_token(cfg)


def form_post(srv, path, fields, headers=None):
    import urllib.parse
    body = urllib.parse.urlencode(fields).encode()
    h = {"Content-Type": "application/x-www-form-urlencoded"}
    h.update(headers or {})
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request("POST", path, body=body, headers=h)
    resp = conn.getresponse()
    raw = resp.read()
    loc = resp.getheader("Location", "")
    conn.close()
    return resp.status, raw, loc


def test_console_requires_auth_and_csrf(server, cfg):
    srv, store = server
    auth = basic(cfg.admin_token)
    # no auth at all
    assert form_post(srv, "/actions/node/add", {"node_id": "n1", "owner": "e"})[0] == 401
    # authed but no csrf
    assert form_post(srv, "/actions/node/add", {"node_id": "n1", "owner": "e"}, auth)[0] == 403
    # authed but wrong csrf
    assert form_post(srv, "/actions/node/add",
                     {"node_id": "n1", "owner": "e", "csrf": "0" * 64}, auth)[0] == 403
    assert store.list_nodes() == []


def test_console_add_shows_token_once_then_node_exists(server, cfg):
    srv, store = server
    status, body, _ = form_post(srv, "/actions/node/add",
                                {"node_id": "node-a", "owner": "erik", "region": "us",
                                 "rc_expected": "1", "csrf": csrf_for(cfg)},
                                basic(cfg.admin_token))
    assert status == 200
    page = body.decode()
    assert "CCFLEET_NODE_ID=node-a" in page and "CCFLEET_NODE_TOKEN=" in page
    node = store.get_node("node-a")
    assert node["owner"] == "erik" and node["rc_expected"] is True
    # the token in the page is a real working node token
    token = re.search(r"CCFLEET_NODE_TOKEN=([0-9a-f]{64})", page).group(1)
    assert store.node_for_token(token)["id"] == "node-a"


def test_console_add_rejects_a_bad_node_id(server, cfg):
    srv, store = server
    status, body, _ = form_post(srv, "/actions/node/add",
                                {"node_id": "Bad Id!", "owner": "erik", "csrf": csrf_for(cfg)},
                                basic(cfg.admin_token))
    assert status == 400 and store.list_nodes() == []
    assert b"<script>" not in body


def test_console_node_actions(server, cfg):
    srv, store = server
    store.add_node("node-a", "erik", now=1.0)
    auth = basic(cfg.admin_token)
    csrf = csrf_for(cfg)

    for action, check in (("disable", lambda n: n["enabled"] is False),
                          ("enable", lambda n: n["enabled"] is True),
                          ("rc-on", lambda n: n["rc_expected"] is True),
                          ("rc-off", lambda n: n["rc_expected"] is False)):
        status, _, loc = form_post(srv, f"/actions/node/node-a/{action}", {"csrf": csrf}, auth)
        assert status == 303 and loc == "/"
        assert check(store.get_node("node-a"))

    status, _, _ = form_post(srv, "/actions/node/node-a/pin",
                             {"csrf": csrf, "version": "2.1.276"}, auth)
    assert status == 303 and store.get_node("node-a")["pinned_version"] == "2.1.276"

    # rotating shows a fresh token and invalidates nothing else
    status, body, _ = form_post(srv, "/actions/node/node-a/rotate-token", {"csrf": csrf}, auth)
    assert status == 200
    new = re.search(r"CCFLEET_NODE_TOKEN=([0-9a-f]{64})", body.decode()).group(1)
    assert store.node_for_token(new)["id"] == "node-a"

    # remove needs the node id typed back
    assert form_post(srv, "/actions/node/node-a/remove", {"csrf": csrf}, auth)[0] == 400
    assert store.get_node("node-a") is not None
    status, _, loc = form_post(srv, "/actions/node/node-a/remove",
                               {"csrf": csrf, "confirm": "node-a"}, auth)
    assert status == 303 and store.get_node("node-a") is None


def test_console_unknown_action_and_unknown_node(server, cfg):
    srv, store = server
    auth = basic(cfg.admin_token)
    csrf = csrf_for(cfg)
    store.add_node("node-a", "erik", now=1.0)
    assert form_post(srv, "/actions/node/node-a/fly", {"csrf": csrf}, auth)[0] == 404
    assert form_post(srv, "/actions/node/ghost/enable", {"csrf": csrf}, auth)[0] == 400


def test_dashboard_carries_the_forms(server, cfg):
    srv, store = server
    store.add_node("node-a", "erik", now=1.0)
    status, body, _ = call(srv, "GET", "/", headers=basic(cfg.admin_token))
    page = body.decode()
    assert status == 200
    assert "Add a node" in page
    assert f'value="{csrf_for(cfg)}"' in page
    assert "/actions/node/node-a/disable" in page


def test_add_result_shows_a_runnable_install_command(server, cfg):
    """The console's whole job here is to hand over one command that works."""
    srv, store = server
    status, body, _ = form_post(srv, "/actions/node/add",
                                {"node_id": "alice-node", "owner": "alice", "region": "us",
                                 "csrf": csrf_for(cfg)}, basic(cfg.admin_token))
    assert status == 200
    page = body.decode()
    token = re.search(r"--token ([0-9a-f]{64})", page).group(1)
    assert store.node_for_token(token)["id"] == "alice-node", "the command carries a working token"
    for part in ("node/install.sh", "sudo bash -s --", "--server", "--node alice-node",
                 "--owner alice"):
        assert part in page, f"install command is missing {part}"
    assert "claude" in page and "/status" in page, "the sign-in step must still be spelled out"


def test_rotate_token_result_keeps_the_owner_in_the_command(server, cfg):
    srv, store = server
    store.add_node("bob-node", "bob", now=1.0)
    status, body, _ = form_post(srv, "/actions/node/bob-node/rotate-token",
                                {"csrf": csrf_for(cfg)}, basic(cfg.admin_token))
    assert status == 200
    assert "--owner bob" in body.decode()


def test_heartbeat_response_carries_desired_state(server):
    """An agent that cannot find `desired` has nothing to reconcile against."""
    srv, store = server
    token = store.add_node("node-a", "erik", pinned_version="2.1.92", rc_expected=True)
    auth = {"Authorization": f"Bearer {token}"}
    payload = heartbeat(time.time())["payload"]
    reply = json.loads(call(srv, "POST", "/api/heartbeat", payload, auth)[1])
    assert reply["desired"] == {"claude_version": "2.1.92", "remote_control": True,
                                "login": None, "poll_s": 300}
    # Agents predating the block read the flat field; it stays while they exist.
    assert reply["pinned_version"] == "2.1.92"


def test_reconcile_result_is_stored_and_junk_around_it_is_dropped(server):
    srv, store = server
    token = store.add_node("node-b", "erik")
    auth = {"Authorization": f"Bearer {token}"}
    payload = {**heartbeat(time.time())["payload"], "node_id": "node-b",
               "reconcile": {"upgrade": {"from": "2.1.90", "to": "2.1.92", "ok": True,
                                         "ts": 1.0, "error": None,
                                         "smuggled": "x" * 5000},
                             "other": {"whatever": 1}}}
    assert call(srv, "POST", "/api/heartbeat", payload, auth)[0] == 200
    stored = store.recent_heartbeats("node-b")[0]["payload"]["reconcile"]["upgrade"]
    assert stored == {"from": "2.1.90", "to": "2.1.92", "ok": True, "ts": 1.0, "error": None}
    assert "smuggled" not in json.dumps(store.recent_heartbeats("node-b")[0]["payload"])
