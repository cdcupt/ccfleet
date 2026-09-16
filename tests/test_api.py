import base64
import http.client
import json
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
