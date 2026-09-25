"""The console's node cards, for a shared machine.

Root on a shared machine runs no Claude Code and is signed in to nothing: its
slot is. The fleet table read the machine's own fields and said "-" and
"unknown" beside a slot that was signed in and in use, offered the operator a
sign-in that the machine's agent never answers, and went on calling a machine
by its id after its slot, its hostname and claude.ai had all taken the
holder's name. These tests hold the console to what the slot says.
"""

from __future__ import annotations

import base64
import http.client
import re
import threading
import time
import urllib.parse

import pytest

from ccfleetd import slots
from ccfleetd.api import Context, build_server, csrf_token
from ccfleetd.config import Config
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.render import SHARED_ELSEWHERE, build_rows, render_dashboard
from ccfleetd.store import Store

ADMIN_TOKEN = "admin-token-long-enough-to-pass"


@pytest.fixture
def console():
    cfg = Config(bind_host="127.0.0.1", bind_port=0, db_path=":memory:",
                 admin_token=ADMIN_TOKEN)
    store = Store(":memory:", max_slots_per_machine=8)
    srv = build_server(Context(store, cfg, Monitor(store, cfg, LogNotifier())),
                       host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    def call(method, path, form=None):
        headers = {"Authorization": "Basic "
                   + base64.b64encode(f"admin:{ADMIN_TOKEN}".encode()).decode()}
        body = None
        if form is not None:
            body = urllib.parse.urlencode({"csrf": csrf_token(cfg), **form}).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Content-Length"] = str(len(body))
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        conn.request(method, path, body=body, headers=headers)
        reply = conn.getresponse()
        reply.body = reply.read().decode("utf-8", "replace")
        conn.close()
        return reply

    yield store, call
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)
    store.close()


USAGE = {"total_tokens": 39_500, "input_tokens": 9_000, "output_tokens": 500,
         "cache_read_input_tokens": 30_000, "sessions": 3, "window_days": 7,
         "by_day": [{"day": "2026-09-24", "total_tokens": 39_500}]}
QUOTA = {"checked_at": None, "session": {"used_pct": 4, "resets": "3:10pm"},
         "week": {"used_pct": 25, "resets": "Sep 26"}}


def machine_beat(store, node, when, reports):
    """What a machine agent sends: nothing of Claude Code at the top, its slots below."""
    store.insert_heartbeat(node, when, {
        "node_id": node, "mode": slots.MACHINE_MODE, "hostname": node,
        "claude": {"version": None}, "credentials": {"present": None},
        "remote_control": {"state": None}, "usage": {}, "quota": {},
        "slots": reports})


def fleet(store, now):
    """Shaped like the live one: an own node, a machine in use, a customer's
    machine still waiting for its sign-in, and a machine with no slot yet."""
    erik = store.upsert_account_from_google("sub-erik", "cdcupt@gmail.com", now=now - 86400)
    store.set_slot_quota(erik["id"], 3)
    store.set_account_handle(erik["id"], "erik")
    ysf = store.upsert_account_from_google("sub-ysf", "ysflowerdog@gmail.com", now=now - 86400)
    store.set_slot_quota(ysf["id"], 1)
    store.set_account_handle(ysf["id"], "ysflowerdog")      # as live, from before neutral names

    store.add_node("erik-1", "erik", region="us-residential-att", now=now - 9 * 86400)
    store.hold_owner_node("erik-1", erik["id"], unix_user="erik", now=now - 16 * 3600)
    store.insert_heartbeat("erik-1", now - 19, {
        "node_id": "erik-1", "claude": {"version": "2.1.281"},
        "credentials": {"present": True, "subscription_type": "max", "mtime": now - 1800,
                        "expires_at": (now + 4 * 3600) * 1000},
        "remote_control": {"state": "active"}})

    for node in ("erik-2", "pool-1"):
        store.add_node(node, "erik", region="us-west-residential", now=now - 5 * 86400)
        store.set_machine_capacity(node, 1)
        store.add_slot(node, node, "slot01", now=now - 5 * 86400)
        store.apply_slot_report(node, [{"unix_user": "slot01", "present": False}],
                                now=now - 4 * 86400)
    held = store.claim_slot(erik["id"], now=now - 20 * 3600, node_id="erik-2")
    store.apply_slot_report("erik-2", [{"unix_user": "slot01", "present": True,
                                        "provisioned_for": held["claimed_at"]}], now=now - 20 * 3600)
    store.apply_slot_report("erik-2", [{"unix_user": "slot01", "present": True,
                                        "credentials": {"logged_in": True}}], now=now - 19 * 3600)
    # As live: this slot predates names, so it goes by its id.
    store._conn.execute("UPDATE slots SET name = NULL WHERE id = 'erik-2'")
    store._conn.commit()
    claimed = store.claim_slot(ysf["id"], now=now - 2 * 3600, node_id="pool-1")
    store.apply_slot_report("pool-1", [{"unix_user": "slot01", "present": True,
                                        "provisioned_for": claimed["claimed_at"]}],
                            now=now - 2 * 3600)

    machine_beat(store, "erik-2", now - 53, [
        # A report for a user the machine no longer has a slot for comes first,
        # so reading "the first report" would describe the wrong account.
        {"unix_user": "slot09", "present": True, "claude": {"version": "9.9.9"},
         "credentials": {"present": False}, "remote_control": {"state": "failed"}},
        {"unix_user": "slot01", "present": True, "claude": {"version": "2.1.281"},
         "credentials": {"present": True, "logged_in": True, "subscription_type": "max",
                         "mtime": now - 2 * 3600, "expires_at": (now + 5 * 3600) * 1000},
         "remote_control": {"state": "active"}, "usage": USAGE, "quota": QUOTA}])
    machine_beat(store, "pool-1", now - 50, [
        {"unix_user": "slot01", "present": True, "claude": {"version": "2.1.281"},
         "credentials": {"present": False, "logged_in": False},
         "remote_control": {"state": "inactive"}, "usage": {}, "quota": {}}])

    store.add_node("pool-2", "erik", region="us-west-residential", now=now - 3600)
    machine_beat(store, "pool-2", now - 40, [])
    # Declared, and never heard from: a machine by the server's own record alone.
    store.add_node("pool-3", "erik", region="us-west-residential", now=now - 600)
    store.set_machine_capacity("pool-3", 1)
    store.add_slot("pool-3", "pool-3", "slot01", now=now - 600)
    return erik, ysf


def table(page):
    return page[page.index("<table"):page.index("</table>")]


def row_of(page, name):
    rows = re.findall(r"<tr class=.*?</tr>", table(page), re.S)
    found = [r for r in rows if f'node-id">{name}<' in r]
    assert len(found) == 1, (name, len(found))
    return found[0]


def card(page, anchor):
    start = page.index(f'id="{anchor}"' if anchor != "usage" else "<h2>Usage and quota</h2>")
    end = page.find("<h2", start + 1)
    return page[start:end if end > 0 else len(page)]


def manage_line(page, name):
    lines = re.findall(r'<div class="row-line"><div class="row-name">.*?</div></div>',
                       card(page, "manage"), re.S)
    found = [ln for ln in lines if f'row-name">{name}<' in ln]
    assert len(found) == 1, (name, len(found))
    return found[0]


def test_a_shared_machines_row_says_what_its_slot_runs(console):
    store, call = console
    now = time.time()
    fleet(store, now)
    store.set_rc_expected("erik-2", True)
    row = row_of(call("GET", "/admin").body, "erik-2")
    assert "2.1.281" in row and "9.9.9" not in row
    assert "Max · refreshed 2.0h ago" in row and "unknown" not in row
    assert "token in 5.0h" in row
    # Remote Control is the slot's; nothing is expected of the machine itself.
    assert ">active<" in row and "(expected)" not in row


def test_an_own_node_still_reads_its_own_facts(console):
    store, call = console
    fleet(store, time.time())
    store.set_rc_expected("erik-1", True)
    row = row_of(call("GET", "/admin").body, "erik-1")
    assert "2.1.281" in row and "Max · refreshed 30m ago" in row
    assert "active (expected)" in row


def byline(row):
    """The small line under a row's name in the fleet table."""
    return re.search(r'node-id">[^<]*</span>.*?<br><span class="muted">(.*?)</span>',
                     row, re.S).group(1)


def test_a_held_slot_is_said_by_its_holders_name_alone(console):
    """Erik, 2026-09-24: while somebody holds a slot it is their name and
    nothing else, not the machine it is on nor the operator who owns it."""
    store, call = console
    fleet(store, time.time())
    page = call("GET", "/admin").body
    row = row_of(page, "ysflowerdog-1")
    assert byline(row) == "us-west-residential"
    assert "pool-1" not in row
    assert 'row-name">ysflowerdog-1</div>' in manage_line(page, "ysflowerdog-1")


def test_what_nobody_holds_by_name_keeps_its_owner(console):
    """A free machine, a slot from before names and an owner's own node go by
    their own ids, with their owner beside them."""
    store, call = console
    fleet(store, time.time())
    page = call("GET", "/admin").body
    for name in ("pool-2", "erik-2", "erik-1"):
        assert byline(row_of(page, name)).startswith("erik \u00b7 "), name
        assert "on " not in byline(row_of(page, name)), name
        assert f'row-name">{name}<span class="muted"> · erik</span>' in manage_line(page, name)


def test_a_slot_given_back_answers_to_its_pool_name_again(console):
    store, call = console
    fleet(store, time.time())
    store.begin_release("pool-1")
    store.apply_slot_report("pool-1", [{"unix_user": "slot01", "present": False}],
                            now=time.time())
    assert store.get_slot("pool-1")["state"] == slots.FREE
    page = call("GET", "/admin").body
    assert byline(row_of(page, "pool-1")) == "erik \u00b7 us-west-residential"
    assert "ysflowerdog-1" not in page


def test_a_slot_nobody_has_signed_in_says_so(console):
    store, call = console
    fleet(store, time.time())
    row = row_of(call("GET", "/admin").body, "ysflowerdog-1")
    assert "not signed in" in row and "missing" not in row and "unknown" not in row
    assert ">inactive<" in row


def test_a_machine_with_no_slot_yet_says_so(console):
    store, call = console
    fleet(store, time.time())
    assert "no slot yet" in row_of(call("GET", "/admin").body, "pool-2")


def test_several_slots_are_left_to_the_slots_card(cfg):
    now = 3_000_000.0
    nodes = [{"id": "legacy-1", "owner": "erik", "region": "us", "pinned_version": "latest",
              "rc_expected": False, "enabled": True, "created_at": 0}]
    latest = {"legacy-1": {"ts": now - 30, "payload": {
        "mode": slots.MACHINE_MODE, "slots": [
            {"unix_user": "slot01", "claude": {"version": "2.1.281"},
             "credentials": {"present": True, "subscription_type": "max"}},
            {"unix_user": "slot02", "claude": {"version": "2.1.273"},
             "credentials": {"present": False}}]}}}
    two = [{"id": "legacy-1-01", "node_id": "legacy-1", "unix_user": "slot01",
            "kind": slots.MACHINE_SLOT, "name": "ana-1"},
           {"id": "legacy-1-02", "node_id": "legacy-1", "unix_user": "slot02",
            "kind": slots.MACHINE_SLOT, "name": None}]
    row = build_rows(nodes, latest, [], now, slots=two)[0]
    assert row["claude_version"] is None and row["credentials_present"] is None
    assert row["name"] == "legacy-1"
    page = render_dashboard([row], [], now, cfg)
    assert "2 slots, see Slots" in page


def test_a_machine_not_heard_from_yet_is_still_a_machine(cfg):
    now = 3_000_000.0
    nodes = [{"id": "pool-3", "owner": "erik", "region": "us", "pinned_version": "latest",
              "rc_expected": False, "enabled": True, "created_at": 0}]
    one = [{"id": "pool-3", "node_id": "pool-3", "unix_user": "slot01",
            "kind": slots.MACHINE_SLOT, "name": None}]
    row = build_rows(nodes, {}, [], now, slots=one)[0]
    assert row["machine"] is True
    page = render_dashboard([row], [], now, cfg, csrf="c")
    assert 'id="sign-in"' not in page and 'id="device-tokens"' not in page


def test_sign_in_and_device_tokens_list_only_owner_nodes(console):
    store, call = console
    fleet(store, time.time())
    page = call("GET", "/admin").body
    for anchor in ("sign-in", "device-tokens"):
        part = card(page, anchor)
        assert 'row-name">erik-1<' in part
        for machine in ("erik-2", "ysflowerdog-1", "pool-1", "pool-2", "pool-3"):
            assert f'row-name">{machine}<' not in part, (anchor, machine)
        assert SHARED_ELSEWHERE in part


def test_the_note_is_only_there_when_a_machine_was_left_out(cfg):
    now = 3_000_000.0
    nodes = [{"id": "node-a", "owner": "erik", "region": "us", "pinned_version": "",
              "rc_expected": False, "enabled": True, "created_at": 0}]
    page = render_dashboard(build_rows(nodes, {}, [], now), [], now, cfg, csrf="c")
    assert 'id="sign-in"' in page and SHARED_ELSEWHERE not in page


@pytest.mark.parametrize("action", ["login-start", "token-start"])
@pytest.mark.parametrize("node", ["erik-2", "pool-1", "pool-2", "pool-3"])
def test_the_server_refuses_a_sign_in_on_a_shared_machine(console, action, node):
    store, call = console
    fleet(store, time.time())
    reply = call("POST", f"/actions/node/{node}/{action}", form={"email": ""})
    assert reply.status == 400
    assert "is a shared machine" in reply.body
    assert store.get_login(node) is None, "nothing was asked of the machine"


@pytest.mark.parametrize("action", ["login-start", "token-start"])
def test_an_own_node_can_still_be_signed_in_from_the_console(console, action):
    store, call = console
    fleet(store, time.time())
    reply = call("POST", f"/actions/node/erik-1/{action}", form={"email": ""})
    assert reply.status in (302, 303)
    assert store.get_login("erik-1") is not None


def test_usage_shows_a_slots_use_under_its_name(console):
    store, call = console
    fleet(store, time.time())
    part = card(call("GET", "/admin").body, "usage")
    assert "erik-2" in part and "39.5k" in part and "tokens run on this slot" in part
    assert "25%" in part, "the slot's weekly window"
    # The customer's slot has used nothing yet: named, by its own name, as quiet.
    assert re.search(r"No usage reported yet from .*ysflowerdog-1", part, re.S)
    assert "pool-1" not in re.search(r"No usage reported yet from .*?</p>", part, re.S).group(0)


def test_a_slots_use_is_under_the_slots_name(cfg):
    now = 3_000_000.0
    nodes = [{"id": "pool-9", "owner": "erik", "region": "us", "pinned_version": "",
              "rc_expected": False, "enabled": True, "created_at": 0}]
    one = [{"id": "pool-9", "node_id": "pool-9", "unix_user": "slot01",
            "kind": slots.MACHINE_SLOT, "name": "ana-1"}]
    latest = {"pool-9": {"ts": now - 5, "payload": {"mode": slots.MACHINE_MODE, "slots": [
        {"unix_user": "slot01", "usage": {"total_tokens": 1200, "by_day": []}}]}}}
    page = render_dashboard(build_rows(nodes, latest, [], now, slots=one), [], now, cfg)
    usage = page[page.index("<h2>Usage and quota</h2>"):]
    assert 'usage-name">ana-1<div' in usage
    assert "pool-9" not in usage and "erik" not in usage


def one_machine(cfg, slot_rows, now=3_000_000.0):
    """The console for one machine, m1, with these slots declared on it."""
    nodes = [{"id": "m1", "owner": "erik", "region": "us", "pinned_version": "",
              "rc_expected": False, "enabled": True, "created_at": 0}]
    latest = {"m1": {"ts": now - 5, "payload": {"mode": slots.MACHINE_MODE, "slots": []}}}
    return render_dashboard(build_rows(nodes, latest, [], now, slots=slot_rows), [], now, cfg)


def slot_row(slot_id, user, name=None):
    return {"id": slot_id, "node_id": "m1", "unix_user": user, "kind": slots.MACHINE_SLOT,
            "name": name}


def test_a_free_slot_not_called_as_its_machine_says_which_machine(cfg):
    page = one_machine(cfg, [slot_row("m1-01", "slot01")])
    assert byline(row_of(page, "m1-01")) == "on m1 \u00b7 erik \u00b7 us"


def test_a_machine_of_two_slots_goes_by_its_own_id(cfg):
    """From before one slot per machine: no one slot speaks for it, so the row
    is the machine's, with its owner, even while a slot on it is somebody's."""
    page = one_machine(cfg, [slot_row("m1-01", "slot01", "ana-1"), slot_row("m1-02", "slot02")])
    row = row_of(page, "m1")
    assert byline(row) == "erik \u00b7 us" and "2 slots, see Slots" in row


def test_no_remote_control_alert_switch_for_a_shared_machine(console):
    store, call = console
    fleet(store, time.time())
    page = call("GET", "/admin").body
    assert "RC alert" in manage_line(page, "erik-1")
    for name in ("erik-2", "ysflowerdog-1", "pool-2"):
        line = manage_line(page, name)
        assert "RC alert" not in line and "Remove" in line
    assert 'row-name">ysflowerdog-1</div>' in manage_line(page, "ysflowerdog-1")


def test_an_alert_names_the_machine_by_its_slot(console):
    store, call = console
    now = time.time()
    fleet(store, now)
    store.open_alert("pool-1", "disk_high", "warn", "disk 88% used", now - 60)
    page = call("GET", "/admin").body
    alerts = page[page.index("<h2>Open alerts</h2>"):page.index("<h2", page.index(
        "<h2>Open alerts</h2>") + 5)]
    assert "ysflowerdog-1 · disk_high" in alerts and "pool-1" not in alerts


def test_a_slots_name_is_shown_not_run(cfg):
    now = 3_000_000.0
    nodes = [{"id": "m1", "owner": "erik", "region": "us", "pinned_version": "",
              "rc_expected": False, "enabled": True, "created_at": 0}]
    one = [{"id": "m1", "node_id": "m1", "unix_user": "slot01",
            "kind": slots.MACHINE_SLOT, "name": "<b>x</b>"}]
    latest = {"m1": {"ts": now - 5, "payload": {"mode": slots.MACHINE_MODE, "slots": [
        {"unix_user": "slot01", "usage": {"total_tokens": 10, "by_day": []}}]}}}
    alerts = [{"node_id": "m1", "rule": "disk_high", "level": "warn", "message": "m",
               "opened_at": now}]
    page = render_dashboard(build_rows(nodes, latest, alerts, now, slots=one), alerts, now,
                            cfg, csrf="c")
    assert "<b>x</b>" not in page and "&lt;b&gt;x&lt;/b&gt;" in page
