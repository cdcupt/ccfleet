import base64
import http.client
import json
import re
import threading
import time
import urllib.parse

import pytest

from ccfleetd.api import Context, build_server
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.store import Store
from tests.conftest import heartbeat, next_load


@pytest.fixture
def server(cfg):
    store = Store(":memory:", max_slots_per_machine=8)
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
    assert call(srv, "GET", "/admin")[0] == 401
    assert call(srv, "GET", "/api/nodes", headers=basic("wrong"))[0] == 401
    assert call(srv, "GET", "/api/nodes", headers={"Authorization": "Basic !!!"})[0] == 401
    status, body, ctype = call(srv, "GET", "/admin", headers=basic(cfg.admin_token))
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



def _machine_payload(node_id, slots):
    """A shared machine's heartbeat: machine facts and its slots, no owner login."""
    payload = heartbeat(time.time())["payload"]
    payload["node_id"] = node_id
    for key in ("claude", "credentials", "remote_control"):
        payload.pop(key)
    payload.update(mode="machine", slots=slots)
    return payload


def test_a_machines_heartbeat_moves_its_slots_and_the_reply_says_so(server):
    """Provisioning reported finished, the slot is claimed; the wipe reported
    finished, the slot is free — and the reply to that same heartbeat already
    says so, rather than one beat later."""
    from ccfleetd import slots
    srv, store = server
    token = store.add_node("m1", "op")
    store.set_machine_capacity("m1", 2)
    auth = {"Authorization": f"Bearer {token}"}
    store.add_account("a1", "sub-1", "a@example.com", slot_quota=2, now=1.0)
    for sid, user in (("s1", "slot01"), ("s2", "slot02")):
        store.add_slot(sid, "m1", user, now=1.0)

    # First word from the machine: both users absent, so both may be claimed.
    reply = json.loads(call(srv, "POST", "/api/heartbeat", _machine_payload("m1", [
        {"unix_user": "slot01", "present": False},
        {"unix_user": "slot02", "present": False}]), auth)[1])
    assert reply["desired"]["slots"] == [{"unix_user": "slot01", "state": "free"},
                                         {"unix_user": "slot02", "state": "free"}]
    claimed_at = store.claim_slot("a1", now=time.time())["claimed_at"]
    store.claim_slot("a1", now=time.time())
    store.begin_release("s2")

    reply = json.loads(call(srv, "POST", "/api/heartbeat", _machine_payload("m1", [
        {"unix_user": "slot01", "present": True, "provisioned_for": claimed_at},
        {"unix_user": "slot02", "present": False}]), auth)[1])
    assert store.get_slot("s1")["state"] == slots.CLAIMED
    assert store.get_slot("s2")["state"] == slots.FREE
    assert reply["desired"]["slots"] == [{"unix_user": "slot01", "state": "claimed"},
                                         {"unix_user": "slot02", "state": "free"}]
    # And nothing about an owner login was raised against a machine with none.
    assert reply["open_alerts"] == []


def test_an_ordinary_nodes_heartbeat_touches_no_slots(server):
    """A node reporting as an ordinary node is not heard on slots, even ones
    declared on it: only a machine agent speaks for them."""
    srv, store = server
    token = store.add_node("node-a", "erik")
    store.add_slot("s1", "node-a", "slot01", now=1.0)
    payload = heartbeat(time.time())["payload"]
    payload["slots"] = [{"unix_user": "slot01", "present": False}]
    reply = json.loads(call(srv, "POST", "/api/heartbeat", payload,
                            {"Authorization": f"Bearer {token}"})[1])
    assert store.get_slot("s1")["present"] is None
    assert reply["desired"]["slots"] == [{"unix_user": "slot01", "state": "free"}]

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
        assert status == 303 and loc == "/admin#manage", \
            "back to the card the button is on, not the top of the page"
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
    status, body, _ = call(srv, "GET", "/admin", headers=basic(cfg.admin_token))
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


def test_a_node_cannot_plant_a_javascript_link_in_the_console(server):
    """The login URL is node-supplied and becomes a link an operator clicks."""
    srv, store = server
    token = store.add_node("node-a", "erik")
    auth = {"Authorization": f"Bearer {token}"}
    store.request_login("node-a", "", time.time())
    payload = {**heartbeat(time.time())["payload"],
               "reconcile": {"login": {"state": "url_ready",
                                       "url": "javascript:alert(document.cookie)"}}}
    assert call(srv, "POST", "/api/heartbeat", payload, auth)[0] == 200
    # Refused at the door rather than merely escaped on the way out.
    assert store.get_login("node-a")["url"] == ""
    good = {**payload, "reconcile": {"login": {
        "state": "url_ready", "url": "https://claude.ai/oauth/authorize?code=1"}}}
    call(srv, "POST", "/api/heartbeat", good, auth)
    assert store.get_login("node-a")["url"] == "https://claude.ai/oauth/authorize?code=1"


def test_unfinished_sign_ins_do_not_linger(server, cfg):
    """An abandoned attempt would keep re-offering itself and keep a code stored."""
    from ccfleetd.monitor import LOGIN_MAX_AGE_S
    srv, store = server
    store.add_node("node-a", "erik")
    now = time.time()
    store.request_login("node-a", "a@b.com", now - LOGIN_MAX_AGE_S - 1)
    store.submit_login_code("node-a", "a-code", now - LOGIN_MAX_AGE_S - 1)
    assert store.get_login("node-a") is not None
    from ccfleetd.monitor import Monitor
    from ccfleetd.notify import LogNotifier
    Monitor(store, cfg, LogNotifier()).check_all(now)
    assert store.get_login("node-a") is None, "stale sign-in and its code must be gone"


def test_a_minted_token_is_readable_until_it_is_finished_with(server, cfg):
    """End to end, over HTTP, on the path a real node takes.

    The failure this guards against was not in the flow but beside it: the
    credential was correctly handed over and deleted, while a copy of the whole
    heartbeat that carried it sat in the archive for the retention window.
    """
    srv, store = server
    token = store.add_node("node-a", "erik")
    auth = {"Authorization": f"Bearer {token}"}
    secret = "sk-ant-oat01-" + "Q" * 50

    store.request_login("node-a", "", time.time(), kind="token")
    status, _, _ = call(srv, "POST", "/api/heartbeat", {
        "node_id": "node-a", "hostname": "h",
        "reconcile": {"login": {"state": "ready", "secret": secret,
                                "requested_at": store.get_login("node-a")["requested_at"]}},
    }, auth)
    assert status == 200

    # It arrived where it was meant to.
    assert store.get_login("node-a")["secret"] == secret
    # And nowhere else. Every heartbeat ever stored for this node.
    archived = json.dumps([dict(r) for r in
                           store._conn.execute("SELECT payload FROM heartbeats")])
    assert secret not in archived, "a credential must not outlive its one showing"
    assert "ready" in archived, "the rest of the report is still kept"

    admin = basic(cfg.admin_token)
    form = {**admin, "Content-Type": "application/x-www-form-urlencoded"}

    def press(action):
        return call(srv, "POST", f"/actions/node/node-a/{action}",
                    f"csrf={csrf_for(cfg)}".encode(), form)

    # Shown over HTTP, and shown again: a second machine needs the same token,
    # and minting another for it is a worse answer than reading this one twice.
    status, body, _ = press("token-show")
    assert status == 200 and secret.encode() in body
    status, body, _ = press("token-show")
    assert status == 200 and secret.encode() in body, "still there for the second machine"

    # Until somebody says they are finished with it.
    assert press("token-done")[0] == 303
    assert store.get_login("node-a") is None
    status, body, _ = press("token-show")
    assert status == 200 and secret.encode() not in body
    assert b"Nothing to show" in body


def test_the_secret_stops_travelling_once_it_has_been_handed_over(server, monkeypatch):
    """The store redaction protects the archive. This protects everything else.

    After the one call that needs it, the credential is dropped from the payload
    the rest of the request works on — rule evaluation, alert messages, anything
    added later. Those have no business seeing it, and the cheapest way to keep
    it that way is for it not to be there.
    """
    srv, store = server
    token = store.add_node("node-a", "erik")
    secret = "sk-ant-oat01-" + "W" * 50
    store.request_login("node-a", "", time.time(), kind="token")

    seen = []
    original = store.__class__.insert_heartbeat

    def spy(self, node_id, ts, payload):
        seen.append(json.dumps(payload))
        return original(self, node_id, ts, payload)

    monkeypatch.setattr(store.__class__, "insert_heartbeat", spy)
    call(srv, "POST", "/api/heartbeat", {
        "node_id": "node-a", "hostname": "h",
        "reconcile": {"login": {"state": "ready", "secret": secret,
                                "requested_at": store.get_login("node-a")["requested_at"]}},
    }, {"Authorization": f"Bearer {token}"})

    assert seen, "the heartbeat was recorded"
    assert secret not in seen[0], "it was already gone before anything downstream saw it"
    assert "ready" in seen[0]
    assert store.get_login("node-a")["secret"] == secret, "and it still reached its one home"


def test_an_action_returns_you_to_the_card_you_used(server, cfg):
    """A 303 to "/" is the top of the page; the cards are two screens down."""
    srv, store = server
    store.add_node("node-a", "erik")
    admin = basic(cfg.admin_token)
    headers = {**admin, "Content-Type": "application/x-www-form-urlencoded"}

    def press(action, extra=""):
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        conn.request("POST", f"/actions/node/node-a/{action}",
                     body=f"csrf={csrf_for(cfg)}{extra}".encode(), headers=headers)
        resp = conn.getresponse()
        resp.read()
        loc = resp.getheader("Location")
        conn.close()
        return resp.status, loc

    assert press("token-start") == (303, "/admin#device-tokens")

    # login-code and login-cancel serve both cards; the row in flight decides
    # which. A cancel deletes that row, so the answer has to be read before the
    # action runs — that ordering is the point of these two assertions.
    store.request_login("node-a", "", time.time(), kind="token")
    assert press("login-cancel") == (303, "/admin#device-tokens")
    assert store.get_login("node-a") is None, "and it really was cancelled"

    store.request_login("node-a", "", time.time(), kind="login")
    assert press("login-cancel") == (303, "/admin#sign-in")

    # Nothing in flight: a sign-in action belongs to the sign-in card.
    assert press("login-start") == (303, "/admin#sign-in")
    store.clear_login("node-a")

    # Management actions have a card too, and it is the furthest down of all
    # of them — these are the buttons pressed several times in a row.
    assert press("rc-on") == (303, "/admin#manage")
    assert press("disable") == (303, "/admin#manage")
    assert press("enable") == (303, "/admin#manage")
    assert press("pin", "&version=2.1.278") == (303, "/admin#manage")


def test_after_an_action_the_console_really_comes_back(server, cfg):
    """An action lands on /admin#<card>. A refresh naming no address there is a
    fragment navigation — the browser scrolls and loads nothing — so after any
    press the console stopped keeping itself current until reloaded by hand."""
    srv, store = server
    store.add_node("node-a", "erik")
    admin = basic(cfg.admin_token)
    status, _, at = form_post(srv, "/actions/node/node-a/rc-on", {"csrf": csrf_for(cfg)}, admin)
    assert status == 303 and "#" in at, "an action lands on its own card"
    status, page, _ = call(srv, "GET", urllib.parse.urldefrag(at)[0], headers=admin)
    assert status == 200
    assert next_load(at, page.decode()) == "/admin"


# -- slot model v2: a machine answers to its slot's name ---------------------------------

def _post(srv, token, payload):
    return json.loads(call(srv, "POST", "/api/heartbeat", payload,
                           {"Authorization": f"Bearer {token}"})[1])


def test_a_machine_is_told_to_answer_to_its_slots_name(server):
    """Free, the slot is called by its id; claimed, by its holder's name — and
    the machine is told to take that as its hostname, since claude.ai/code
    shows a machine by its hostname."""
    srv, store = server
    token = store.add_node("pool-1", "op")
    store.add_slot("pool-1", "pool-1", "slot01", now=1.0)
    store.add_account("a1", "sub-1", "alice@example.com", slot_quota=1, now=1.0)
    store.set_account_handle("a1", "alice")
    free = [{"unix_user": "slot01", "present": False}]
    assert _post(srv, token, _machine_payload("pool-1", free))["desired"]["hostname"] == "pool-1"
    store.claim_slot("a1", now=time.time())
    assert _post(srv, token, _machine_payload("pool-1", free))["desired"]["hostname"] == "alice-1"


def test_a_machine_with_no_slot_is_told_its_own_id(server):
    srv, store = server
    token = store.add_node("pool-9", "op")
    assert _post(srv, token, _machine_payload("pool-9", []))["desired"]["hostname"] == "pool-9"


def test_a_slot_id_that_is_no_hostname_falls_back_to_the_machines(server):
    srv, store = server
    token = store.add_node("pool-2", "op")
    store.add_slot("pool-2-", "pool-2", "slot01", now=1.0)
    reply = _post(srv, token, _machine_payload("pool-2", []))
    assert reply["desired"]["hostname"] == "pool-2"


def test_an_owner_node_counted_as_a_slot_hears_nothing_about_slots(server):
    """The record is ours; the node is theirs. Its agent is told nothing new:
    no slot to provision, no name to take."""
    srv, store = server
    token = store.add_node("erik-1", "erik")
    store.add_account("e1", "sub-e", "cdcupt@gmail.com", slot_quota=1, now=1.0)
    store.hold_owner_node("erik-1", "e1", now=1.0)
    reply = _post(srv, token, dict(heartbeat(time.time())["payload"], node_id="erik-1"))
    assert "slots" not in reply["desired"] and "hostname" not in reply["desired"]
    assert store.get_slot("erik-1")["state"] == "active"
