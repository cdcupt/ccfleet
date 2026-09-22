"""Signing into a slot: the server's half.

A slot's holder signs into their own Claude account from their page, and the
machine runs Claude Code's own login as the slot's user: a URL out, a code
back, the credential written in that user's home. The same dance as a node's
own sign-in, filed against the slot. What matters most here is where it can
and cannot reach: only a slot that is set up and held, only the machine that
carries it, and nothing of it surviving the slot changing hands.
"""

from __future__ import annotations

import json
import time

import pytest

from ccfleetd import slots
from ccfleetd.desired import desired_state
from ccfleetd.heartbeat import validate_heartbeat
from ccfleetd.store import Store, StoreError, slot_login_key

NOW = 1_700_000_000.0
URL = "https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=y"
TOKEN = "sk-ant-oat01-" + "A" * 40


@pytest.fixture
def store():
    st = Store(":memory:")
    yield st
    st.close()


def held(st, state=slots.ACTIVE, slot_id="s1", machine="m1", user="slot01", account="a1"):
    """A slot on a machine, held by an account, moved to `state`."""
    if st.get_node(machine) is None:
        st.add_node(machine, "op", now=NOW)
        st.set_machine_capacity(machine, 8)
    if st.get_account(account) is None:
        st.add_account(account, f"sub-{account}", f"{account}@example.com",
                       slot_quota=8, now=NOW)
    st.add_slot(slot_id, machine, user, now=NOW)
    st.apply_slot_report(machine, [{"unix_user": user, "present": False}], now=NOW)
    claim = st.claim_slot(account, now=NOW, node_id=machine)["claimed_at"]
    if state in (slots.CLAIMED, slots.ACTIVE, slots.RELEASING):
        st.apply_slot_report(machine, [{"unix_user": user, "present": True,
                                        "provisioned_for": claim}], now=NOW)
    if state in (slots.ACTIVE,):
        st.apply_slot_report(machine, [{"unix_user": user, "present": True,
                                        "credentials": {"logged_in": True}}], now=NOW)
    if state == slots.RELEASING:
        st.begin_release(slot_id)
    assert st.get_slot(slot_id)["state"] == state
    return slot_id


# -- where a sign-in may start --------------------------------------------------

@pytest.mark.parametrize("state", [slots.CLAIMED, slots.ACTIVE])
def test_a_set_up_slot_can_be_signed_into(store, state):
    held(store, state)
    store.request_slot_login("s1", "me@example.com", NOW + 1)
    row = store.get_login(slot_login_key("s1"))
    assert row["state"] == "requested" and row["kind"] == "login"
    assert row["email"] == "me@example.com"


@pytest.mark.parametrize("state", [slots.CLAIMING, slots.RELEASING])
def test_a_slot_not_set_up_or_being_given_back_cannot(store, state):
    """Claiming has no user yet to sign in as; releasing must not start a
    login its next holder could inherit."""
    held(store, state)
    with pytest.raises(StoreError):
        store.request_slot_login("s1", "", NOW + 1)
    assert store.get_login(slot_login_key("s1")) is None


def test_a_free_slot_cannot_be_signed_into(store):
    store.add_node("m1", "op", now=NOW)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    with pytest.raises(StoreError):
        store.request_slot_login("s1", "", NOW)


def test_signing_into_a_slot_that_is_not_there(store):
    with pytest.raises(StoreError):
        store.request_slot_login("ghost", "", NOW)


def test_only_the_two_kinds_of_sign_in_exist(store):
    held(store)
    with pytest.raises(StoreError):
        store.request_slot_login("s1", "", NOW, kind="shell")


def test_a_slots_sign_in_can_never_be_a_nodes(store):
    """Both live in one table. A node id cannot contain a colon, so a node
    called whatever a slot's key is cannot be registered at all."""
    held(store)
    with pytest.raises(StoreError):
        store.add_node(slot_login_key("s1"), "op", now=NOW)


# -- nothing survives the slot changing hands -----------------------------------

def test_giving_a_slot_back_takes_its_sign_in_with_it(store):
    held(store)
    store.request_slot_login("s1", "", NOW + 1)
    store.submit_login_code(slot_login_key("s1"), "the-code", NOW + 2)
    store.begin_release("s1")
    assert store.get_login(slot_login_key("s1")) is None


def test_a_token_nobody_collected_goes_with_the_slot(store):
    """Minted for the person giving the slot back. The next holder is somebody
    else, and must never be able to read it."""
    held(store)
    store.request_slot_login("s1", "", NOW + 1, kind="token")
    key = slot_login_key("s1")
    requested_at = store.get_login(key)["requested_at"]
    store.record_login_progress(key, "ready", "", "", NOW + 3, requested_at, secret=TOKEN)
    assert store.read_slot_secret("s1", NOW + 4) == TOKEN
    store.begin_release("s1")
    assert store.read_slot_secret("s1", NOW + 5) == ""


def test_a_slots_token_is_remembered_on_the_slot_not_the_machine(store):
    held(store)
    store.request_slot_login("s1", "", NOW + 1, kind="token")
    key = slot_login_key("s1")
    store.record_login_progress(key, "ready", "", "", NOW + 3,
                                store.get_login(key)["requested_at"], secret=TOKEN)
    store.read_slot_secret("s1", NOW + 4)
    assert store.get_slot("s1")["device_token_at"] == NOW + 4
    assert store.get_node("m1")["device_token_at"] == 0


def test_a_device_token_record_is_only_ever_on_a_node_or_a_slot(store):
    with pytest.raises(StoreError):
        store._read_secret("x", "accounts", "a1", NOW)


# -- what goes down to the machine ----------------------------------------------

def _row(state, **extra):
    return {"id": "s1", "unix_user": "slot01", "state": state, "claimed_at": NOW, **extra}


def test_a_pending_sign_in_rides_down_with_its_slot():
    login = {"state": "requested", "requested_at": NOW, "email": "", "kind": "token"}
    desired = desired_state({"pinned_version": ""}, slots=[_row("active")],
                            slot_logins={"s1": login})
    assert desired["slots"][0]["login"] == {"requested_at": NOW, "email": "",
                                            "kind": "token"}
    assert desired["poll_s"] == 5, "somebody is waiting on a page for this URL"


@pytest.mark.parametrize("state", ["free", "claiming", "releasing"])
def test_no_sign_in_goes_down_for_a_slot_not_held_and_set_up(state):
    """Even if a row were somehow left behind, a slot being wiped is never
    told to run a login."""
    login = {"state": "requested", "requested_at": NOW, "kind": "login"}
    desired = desired_state({"pinned_version": ""}, slots=[_row(state)],
                            slot_logins={"s1": login})
    assert "login" not in desired["slots"][0]
    assert desired["poll_s"] == 300


def test_a_code_goes_down_only_once_somebody_has_typed_one():
    login = {"state": "code_sent", "requested_at": NOW, "kind": "login", "code": "c0de"}
    assert desired_state({}, slots=[_row("claimed")], slot_logins={"s1": login}
                         )["slots"][0]["login"]["code"] == "c0de"


# -- what comes back up ----------------------------------------------------------

def _machine(slot_entries):
    return validate_heartbeat({"node_id": "m1", "mode": "machine", "slots": slot_entries},
                              "m1")


def test_a_slots_sign_in_progress_is_kept_and_bounded():
    [slot] = _machine([{"unix_user": "slot01", "login": {
        "state": "ready", "url": "u" * 5000, "detail": "d" * 900,
        "requested_at": NOW, "secret": "s" * 5000, "extra": 1}}])["slots"]
    login = slot["login"]
    assert login["state"] == "ready" and login["requested_at"] == NOW
    assert len(login["url"]) == 1024 and len(login["detail"]) == 200
    assert len(login["secret"]) == 512
    assert "extra" not in login


def test_a_slot_with_nothing_to_say_about_signing_in_carries_no_login():
    [slot] = _machine([{"unix_user": "slot01", "login": {"url": URL}}])["slots"]
    assert "login" not in slot


def test_an_archived_heartbeat_keeps_no_slots_token(store):
    """Heartbeats are kept for a month. A token riding up inside one would
    outlive its single showing by thirty days, in a copy nothing points at."""
    store.add_node("m1", "op", now=NOW)
    payload = _machine([{"unix_user": "slot01",
                         "login": {"state": "ready", "requested_at": NOW, "secret": TOKEN}}])
    store.insert_heartbeat("m1", NOW, payload)
    kept = store.recent_heartbeats("m1", 1)[0]["payload"]
    assert TOKEN not in json.dumps(kept)
    assert kept["slots"][0]["login"]["state"] == "ready"
    assert payload["slots"][0]["login"]["secret"] == TOKEN, "redacted the caller's copy"


# -- the round trip through the server -------------------------------------------

@pytest.fixture
def server():
    import threading

    from ccfleetd.api import Context, build_server
    from ccfleetd.config import Config
    from ccfleetd.monitor import Monitor
    from ccfleetd.notify import LogNotifier
    cfg = Config.from_env({"CCFLEET_ADMIN_TOKEN": "x" * 32, "CCFLEET_DB": ":memory:"})
    st = Store(":memory:")
    srv = build_server(Context(st, cfg, Monitor(st, cfg, LogNotifier())),
                       host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, st
    srv.shutdown()
    srv.server_close()
    st.close()


def _post(srv, token, slot_entries, node_id="m1"):
    import http.client
    body = json.dumps({"node_id": node_id, "ts": time.time(), "mode": "machine",
                       "slots": slot_entries}).encode()
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request("POST", "/api/heartbeat", body=body,
                 headers={"Authorization": f"Bearer {token}",
                          "Content-Type": "application/json"})
    reply = conn.getresponse()
    data = reply.read()
    conn.close()
    assert reply.status == 200, data
    return json.loads(data)


def test_a_sign_in_on_a_slot_goes_round_the_whole_loop(server):
    srv, st = server
    held(st, slots.CLAIMED)
    token = st.rotate_token("m1")
    st.request_slot_login("s1", "", NOW, kind="login")
    requested_at = st.get_login(slot_login_key("s1"))["requested_at"]

    reply = _post(srv, token, [{"unix_user": "slot01", "present": True}])
    assert reply["desired"]["slots"][0]["login"]["requested_at"] == requested_at

    _post(srv, token, [{"unix_user": "slot01", "present": True, "login": {
        "state": "url_ready", "url": URL, "requested_at": requested_at}}])
    assert st.get_login(slot_login_key("s1"))["url"] == URL

    st.submit_login_code(slot_login_key("s1"), "the-code", time.time())
    reply = _post(srv, token, [{"unix_user": "slot01", "present": True}])
    assert reply["desired"]["slots"][0]["login"]["code"] == "the-code"

    # Done, and signed in: the row goes, the slot is active, the loop stops.
    reply = _post(srv, token, [{"unix_user": "slot01", "present": True,
                                "credentials": {"logged_in": True},
                                "login": {"state": "done", "requested_at": requested_at}}])
    assert st.get_login(slot_login_key("s1")) is None
    assert st.get_slot("s1")["state"] == slots.ACTIVE
    assert "login" not in reply["desired"]["slots"][0]
    assert reply["desired"]["poll_s"] == 300


def test_a_token_minted_on_a_slot_reaches_its_page_and_no_archive(server):
    srv, st = server
    held(st, slots.ACTIVE)
    token = st.rotate_token("m1")
    st.request_slot_login("s1", "", NOW, kind="token")
    requested_at = st.get_login(slot_login_key("s1"))["requested_at"]
    _post(srv, token, [{"unix_user": "slot01", "present": True, "login": {
        "state": "ready", "secret": TOKEN, "requested_at": requested_at}}])
    assert st.read_slot_secret("s1", time.time()) == TOKEN
    archived = json.dumps([h["payload"] for h in st.recent_heartbeats("m1", 5)])
    assert TOKEN not in archived


def test_a_machine_cannot_move_the_sign_in_of_another_machines_slot(server):
    """The same user name on two machines is two different slots. A report is
    filed against the slot on the machine that sent it, and nowhere else."""
    srv, st = server
    held(st, slots.ACTIVE, slot_id="s1", machine="m1", user="slot01", account="a1")
    held(st, slots.ACTIVE, slot_id="s2", machine="m2", user="slot01", account="a2")
    # m1's slot is the one a careless lookup by user name would find first.
    token_m2 = st.rotate_token("m2")
    st.request_slot_login("s1", "", NOW, kind="token")
    requested_at = st.get_login(slot_login_key("s1"))["requested_at"]
    _post(srv, token_m2, [{"unix_user": "slot01", "present": True, "login": {
        "state": "ready", "secret": TOKEN, "requested_at": requested_at}}], node_id="m2")
    assert st.read_slot_secret("s1", time.time()) == "", "m2 planted a token on m1's slot"
    assert st.get_login(slot_login_key("s1"))["state"] == "requested"


def test_a_report_about_a_slot_nobody_declared_keeps_no_token(server):
    srv, st = server
    held(st, slots.ACTIVE)
    token = st.rotate_token("m1")
    _post(srv, token, [{"unix_user": "ghost01", "present": True,
                        "login": {"state": "ready", "secret": TOKEN, "requested_at": 1.0}}])
    assert TOKEN not in json.dumps([h["payload"] for h in st.recent_heartbeats("m1", 5)])
