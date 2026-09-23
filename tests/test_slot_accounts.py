"""The Claude accounts on a slot: the server's half.

A slot keeps up to three of its holder's own Claude accounts signed in, one of
them active. The sign-ins stay on the machine. The server carries two things
one way (sign this account in, use or forget that one) and what happened the
other. What matters most is reach: only the holder, only a slot that is set
up, only the machine that carries it. After that, nothing asked for may
outlive the slot changing hands, and a machine's word is accepted only in the
shapes it is allowed to take.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from ccfleetd import slots
from ccfleetd.config import Config
from ccfleetd.desired import IDLE_POLL_S, LOGIN_POLL_S, desired_state
from ccfleetd.heartbeat import validate_heartbeat
from ccfleetd.monitor import LOGIN_MAX_AGE_S, Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.store import NotYours, Store, StoreError, slot_login_key

from .test_slot_signin import NOW, held
from .test_slots import _PauseAfter

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def store():
    st = Store(":memory:")
    yield st
    st.close()


# -- who may ask, and when ---------------------------------------------------------

@pytest.mark.parametrize("state", [slots.CLAIMED, slots.ACTIVE])
@pytest.mark.parametrize("action", ["use", "forget"])
def test_the_holder_of_a_set_up_slot_can_ask(store, state, action):
    held(store, state)
    store.request_account_action("s1", action, "2", NOW + 1, held_by="a1")
    assert store.get_account_intent("s1") == {
        "slot_id": "s1", "action": action, "account": "2", "requested_at": NOW + 1,
        "state": "requested", "detail": "", "updated_at": NOW + 1}


@pytest.mark.parametrize("state", [slots.CLAIMING, slots.RELEASING])
def test_a_slot_not_set_up_or_on_its_way_out_cannot_be_asked(store, state):
    """Claiming has no accounts yet; releasing must not leave a request for
    whoever holds the slot next."""
    held(store, state)
    with pytest.raises(StoreError):
        store.request_account_action("s1", "use", "2", NOW + 1, held_by="a1")
    assert store.get_account_intent("s1") is None


def test_a_free_slot_cannot_be_asked(store):
    store.add_node("m1", "op", now=NOW)
    store.add_slot("s1", "m1", "slot01", now=NOW)
    with pytest.raises(StoreError):
        store.request_account_action("s1", "use", "2", NOW)


def test_somebody_elses_slot_is_refused_and_left_alone(store):
    held(store)
    store.add_account("a2", "sub-a2", "a2@example.com", slot_quota=1, now=NOW)
    with pytest.raises(NotYours):
        store.request_account_action("s1", "use", "2", NOW + 1, held_by="a2")
    assert store.get_account_intent("s1") is None


@pytest.mark.parametrize("action, account", [
    ("switch", "2"), ("", "2"), ("use", "4"), ("use", "0"), ("use", "new"),
    ("use", ""), ("forget", "1 "), ("use", 2)])
def test_only_the_known_requests_exist(store, action, account):
    held(store)
    with pytest.raises(StoreError):
        store.request_account_action("s1", action, account, NOW + 1, held_by="a1")
    assert store.get_account_intent("s1") is None


def test_a_new_request_replaces_the_last(store):
    held(store)
    store.request_account_action("s1", "use", "2", NOW + 1, held_by="a1")
    store.request_account_action("s1", "forget", "3", NOW + 2, held_by="a1")
    intent = store.get_account_intent("s1")
    assert (intent["action"], intent["account"], intent["requested_at"]) == ("forget", "3", NOW + 2)


def test_the_holder_is_checked_in_the_same_transaction_as_the_write(tmp_path):
    """Checked in one statement and written in the next, a release landing
    between the two leaves a request hanging off a slot on its way to being
    wiped. Its next holder's machine would then act on it. Forced here: the
    request pauses right after its check, and the operator releases the slot
    from a second connection meanwhile."""
    db = str(tmp_path / "fleet.db")
    setup = Store(db)
    try:
        held(setup)
    finally:
        setup.close()

    gate, reached = threading.Event(), threading.Event()
    outcomes: dict[str, str] = {}

    def slow_request():
        own = Store(db)
        own._conn = _PauseAfter(own._conn, gate, "held_by FROM slots WHERE id")
        own._conn.reached = reached
        try:
            own.request_account_action("s1", "use", "2", NOW + 1, held_by="a1")
            outcomes["request"] = "stored"
        except StoreError as exc:
            outcomes["request"] = type(exc).__name__
        finally:
            own.close()

    def release():
        own = Store(db)
        try:
            own.begin_release("s1")
            outcomes["release"] = "done"
        finally:
            own.close()

    asking = threading.Thread(target=slow_request)
    asking.start()
    assert reached.wait(10), "the request never reached its holder check"
    releasing = threading.Thread(target=release)
    releasing.start()
    releasing.join(timeout=1.5)
    gate.set()
    asking.join(timeout=30)
    releasing.join(timeout=30)

    after = Store(db)
    try:
        assert after.get_slot("s1")["state"] == slots.RELEASING, outcomes
        assert after.get_account_intent("s1") is None, f"a request survived: {outcomes}"
    finally:
        after.close()


# -- nothing survives the slot changing hands -------------------------------------

def test_giving_a_slot_back_takes_its_request_with_it(store):
    held(store)
    store.request_account_action("s1", "use", "2", NOW + 1, held_by="a1")
    store.begin_release("s1")
    assert store.get_account_intent("s1") is None


def test_giving_a_slot_back_takes_a_failed_request_with_it(store):
    held(store)
    store.request_account_action("s1", "use", "2", NOW + 1, held_by="a1")
    store.record_account_progress("s1", {"state": "failed", "requested_at": NOW + 1,
                                         "detail": "signed out"}, NOW + 2)
    store.begin_release("s1")
    assert store.get_account_intent("s1") is None


# -- what the machine says about it -------------------------------------------------

def test_done_clears_the_request(store):
    held(store)
    store.request_account_action("s1", "use", "2", NOW + 1, held_by="a1")
    store.record_account_progress("s1", {"state": "done", "requested_at": NOW + 1}, NOW + 5)
    assert store.get_account_intent("s1") is None


def test_failed_is_kept_to_be_said_and_no_longer_asked(store):
    held(store)
    store.request_account_action("s1", "forget", "3", NOW + 1, held_by="a1")
    store.record_account_progress("s1", {"state": "failed", "requested_at": NOW + 1,
                                         "detail": "x" * 500}, NOW + 5)
    intent = store.get_account_intent("s1")
    assert intent["state"] == "failed" and intent["detail"] == "x" * 200
    assert intent["updated_at"] == NOW + 5


@pytest.mark.parametrize("requested_at", [NOW, NOW + 2, None, "later", True])
def test_only_news_about_this_request_clears_it(store, requested_at):
    """The machine acts a beat late by construction. A report about the
    request before this one must not end this one — the holder asked twice,
    and the second is the one they are waiting for."""
    held(store)
    store.request_account_action("s1", "use", "2", NOW + 1, held_by="a1")
    store.record_account_progress("s1", {"state": "done", "requested_at": requested_at},
                                  NOW + 5)
    intent = store.get_account_intent("s1")
    assert intent is not None and intent["state"] == "requested"


@pytest.mark.parametrize("progress", [
    {"state": "requested", "requested_at": NOW + 1}, {"state": "gone", "requested_at": NOW + 1},
    {"requested_at": NOW + 1}, None, "done", ["done"]])
def test_anything_but_done_or_failed_changes_nothing(store, progress):
    held(store)
    store.request_account_action("s1", "use", "2", NOW + 1, held_by="a1")
    store.record_account_progress("s1", progress, NOW + 5)
    assert store.get_account_intent("s1")["state"] == "requested"


def test_a_failure_cannot_be_turned_into_something_else_later(store):
    """Once failed, the request is finished. A late 'done' for it must not
    make the holder's page claim a switch that never happened."""
    held(store)
    store.request_account_action("s1", "use", "2", NOW + 1, held_by="a1")
    store.record_account_progress("s1", {"state": "failed", "requested_at": NOW + 1,
                                         "detail": "signed out"}, NOW + 2)
    store.record_account_progress("s1", {"state": "done", "requested_at": NOW + 1}, NOW + 3)
    assert store.get_account_intent("s1")["state"] == "failed"


def test_news_about_a_slot_with_no_request_is_ignored(store):
    held(store)
    store.record_account_progress("s1", {"state": "done", "requested_at": NOW}, NOW + 1)
    assert store.get_account_intent("s1") is None


# -- signing an account in, somewhere in particular ----------------------------------

@pytest.mark.parametrize("target", ["new", "1", "2", "3"])
def test_a_sign_in_can_name_where_it_lands(store, target):
    held(store)
    store.request_slot_login("s1", "me@example.com", NOW + 1, held_by="a1", account=target)
    assert store.get_login(slot_login_key("s1"))["account"] == target


def test_a_sign_in_that_names_nothing_lands_where_it_always_did(store):
    held(store)
    store.request_slot_login("s1", "", NOW + 1, held_by="a1")
    assert store.get_login(slot_login_key("s1"))["account"] == ""


@pytest.mark.parametrize("target", ["4", "old", "NEW", " new", "0"])
def test_a_sign_in_cannot_name_anywhere_else(store, target):
    held(store)
    with pytest.raises(StoreError):
        store.request_slot_login("s1", "", NOW + 1, held_by="a1", account=target)
    assert store.get_login(slot_login_key("s1")) is None


def test_a_device_token_does_not_name_an_account(store):
    """A token is minted from whichever account is active; naming another
    would promise something the machine does not do."""
    held(store)
    with pytest.raises(StoreError):
        store.request_slot_login("s1", "", NOW + 1, kind="token", held_by="a1", account="new")


def test_a_later_sign_in_does_not_inherit_an_earlier_ones_target(store):
    held(store)
    store.request_slot_login("s1", "", NOW + 1, held_by="a1", account="new")
    store.request_slot_login("s1", "", NOW + 2, held_by="a1")
    assert store.get_login(slot_login_key("s1"))["account"] == ""


# -- what the machine is told -------------------------------------------------------

def machine_desired(store, machine="m1"):
    slot_rows = store.list_slots(node_id=machine)
    return desired_state(
        store.get_node(machine), None, slot_rows,
        {s["id"]: store.get_login(slot_login_key(s["id"])) for s in slot_rows},
        {s["id"]: store.get_account_intent(s["id"]) for s in slot_rows})


def test_a_waiting_request_rides_to_the_machine_and_hurries_it(store):
    held(store)
    store.request_account_action("s1", "use", "2", NOW + 1, held_by="a1")
    desired = machine_desired(store)
    [block] = desired["slots"]
    assert block["account"] == {"action": "use", "id": "2", "requested_at": NOW + 1}
    assert desired["poll_s"] == LOGIN_POLL_S


def test_a_failed_request_is_not_asked_again(store):
    held(store)
    store.request_account_action("s1", "use", "2", NOW + 1, held_by="a1")
    store.record_account_progress("s1", {"state": "failed", "requested_at": NOW + 1}, NOW + 2)
    desired = machine_desired(store)
    assert "account" not in desired["slots"][0]
    assert desired["poll_s"] == IDLE_POLL_S


def test_without_a_request_the_machines_reply_keeps_its_shape(store):
    held(store)
    [block] = machine_desired(store)["slots"]
    assert set(block) == {"unix_user", "state"}


def test_a_request_is_only_ever_sent_for_a_set_up_slot():
    """However the row came to exist, a slot that is not claimed or active is
    never told to switch."""
    intent = {"action": "use", "account": "2", "requested_at": NOW, "state": "requested"}
    for state in (slots.FREE, slots.CLAIMING, slots.RELEASING):
        slot = {"id": "s1", "unix_user": "slot01", "state": state, "claimed_at": NOW}
        desired = desired_state({"pinned_version": ""}, None, [slot], {}, {"s1": intent})
        assert "account" not in desired["slots"][0], state


@pytest.mark.parametrize("bad", [{"action": "switch"}, {"account": "9"}, {"account": 2},
                                 {"state": "failed"}])
def test_only_a_well_formed_waiting_request_is_sent(bad):
    intent = {"action": "use", "account": "2", "requested_at": NOW, "state": "requested",
              **bad}
    slot = {"id": "s1", "unix_user": "slot01", "state": slots.ACTIVE}
    desired = desired_state({"pinned_version": ""}, None, [slot], {}, {"s1": intent})
    assert "account" not in desired["slots"][0]


def test_a_sign_in_carries_where_it_lands(store):
    held(store)
    store.request_slot_login("s1", "me@example.com", NOW + 1, held_by="a1", account="new")
    [block] = machine_desired(store)["slots"]
    assert block["login"]["account"] == "new"


def test_a_sign_in_that_names_nothing_is_sent_exactly_as_before(store):
    held(store)
    store.request_slot_login("s1", "", NOW + 1, held_by="a1")
    [block] = machine_desired(store)["slots"]
    assert set(block["login"]) == {"requested_at", "email", "kind"}


def test_a_stored_target_the_machine_would_not_know_is_not_sent():
    login = {"state": "requested", "requested_at": NOW, "email": "", "kind": "login",
             "account": "7"}
    slot = {"id": "s1", "unix_user": "slot01", "state": slots.ACTIVE}
    desired = desired_state({"pinned_version": ""}, None, [slot], {"s1": login})
    assert "account" not in desired["slots"][0]["login"]


def test_a_nodes_own_sign_in_never_names_an_account(store):
    store.add_node("n1", "erik", now=NOW)
    store.request_login("n1", "", NOW + 1)
    desired = desired_state(store.get_node("n1"), store.get_login("n1"))
    assert "account" not in desired["login"]


# -- what the machine may say -------------------------------------------------------

def beat(**slot_fields):
    payload = {"node_id": "m1", "mode": "machine",
               "slots": [{"unix_user": "slot01", "present": True, **slot_fields}]}
    return validate_heartbeat(json.loads(json.dumps(payload)), "m1")["slots"][0]


GOOD = {"id": "1", "email": "me@example.com", "plan": "max", "active": True,
        "signed_in": True, "refresh_expires_at": NOW + 86400}


def test_a_slots_accounts_are_kept_as_reported():
    other = {**GOOD, "id": "2", "email": "work@example.org", "plan": "pro", "active": False}
    assert beat(accounts=[GOOD, other])["accounts"] == [GOOD, other]


def test_a_slot_that_reports_no_accounts_keeps_the_shape_it_had():
    assert "accounts" not in beat()
    assert "account_switch" not in beat()


@pytest.mark.parametrize("bad_id", ["4", "0", "01", "", 1, None, ["1"], "new"])
def test_an_account_the_slot_cannot_have_is_dropped(bad_id):
    assert beat(accounts=[{**GOOD, "id": bad_id}])["accounts"] == []


def test_an_account_reported_twice_keeps_its_first_entry():
    twice = [GOOD, {**GOOD, "email": "someone-else@example.com"}]
    assert beat(accounts=twice)["accounts"] == [GOOD]


def test_no_more_than_three_accounts_are_kept():
    many = [{**GOOD, "id": i, "active": False} for i in ("1", "2", "3", "1", "2")]
    kept = beat(accounts=many)["accounts"]
    assert [a["id"] for a in kept] == ["1", "2", "3"]


@pytest.mark.parametrize("bad_email", [
    "no-at-sign", "a@b", "<script>@x.com", 'a"b@example.com', "a b@example.com",
    "a@example.com\n", "tab\t@example.com", "bell\x07@example.com", "@example.com",
    "x" * 65 + "@example.com", "a@" + "b" * 250 + ".com", 42, None])
def test_an_address_that_is_not_one_is_not_kept(bad_email):
    [account] = beat(accounts=[{**GOOD, "email": bad_email}])["accounts"]
    assert account["email"] == ""


def test_a_long_address_that_is_still_one_is_kept_whole():
    address = "a" * 60 + "@" + "b" * 180 + ".example.com"
    assert len(address) <= 254
    [account] = beat(accounts=[{**GOOD, "email": address}])["accounts"]
    assert account["email"] == address


def test_the_plan_is_bounded():
    [account] = beat(accounts=[{**GOOD, "plan": "p" * 100}])["accounts"]
    assert account["plan"] == "p" * 40
    [account] = beat(accounts=[{**GOOD, "plan": 3}])["accounts"]
    assert account["plan"] is None


@pytest.mark.parametrize("flag", ["yes", 1, "true", None])
def test_active_and_signed_in_are_strict(flag):
    [account] = beat(accounts=[{**GOOD, "active": flag, "signed_in": flag}])["accounts"]
    assert account["active"] is False
    assert account["signed_in"] is None


def test_only_one_account_can_be_active():
    both = [GOOD, {**GOOD, "id": "2"}]
    assert [a["active"] for a in beat(accounts=both)["accounts"]] == [True, False]


@pytest.mark.parametrize("when", ["soon", True, 10 ** 30, float("nan"), float("inf")])
def test_an_expiry_that_is_not_a_sane_number_is_not_kept(when):
    raw = {**GOOD, "refresh_expires_at": when}
    payload = {"node_id": "m1", "mode": "machine",
               "slots": [{"unix_user": "slot01", "accounts": [raw]}]}
    [account] = validate_heartbeat(payload, "m1")["slots"][0]["accounts"]
    assert account["refresh_expires_at"] is None


def test_only_the_fields_named_are_kept():
    [account] = beat(accounts=[{**GOOD, "accessToken": "sk-ant-oat01-x",
                                "refreshToken": "r", "name": "Me"}])["accounts"]
    assert set(account) == set(GOOD)


def test_what_happened_to_a_request_is_kept_in_its_shape():
    said = beat(account_switch={"state": "failed", "requested_at": NOW, "detail": "d" * 400,
                                "extra": 1})["account_switch"]
    assert said == {"state": "failed", "requested_at": NOW, "detail": "d" * 200}


@pytest.mark.parametrize("switch", [
    {"state": "done"}, {"state": "requested", "requested_at": NOW},
    {"state": "done", "requested_at": "now"}, {"state": ["done"], "requested_at": NOW},
    {"state": "done", "requested_at": True}])
def test_news_that_cannot_be_matched_to_a_request_is_dropped(switch):
    assert "account_switch" not in beat(account_switch=switch)


# -- through the real heartbeat -----------------------------------------------------

def test_a_machine_clears_the_requests_of_its_own_slots_and_nobody_elses(tmp_path):
    """Two machines, each with a slot01. The report is matched on the machine
    that sent it, so m1 talking about its slot01 can never finish a request
    made on m2's."""
    import http.client

    from ccfleetd.api import Context, build_server

    store = Store(":memory:")
    cfg = Config(bind_host="127.0.0.1", bind_port=0, db_path=":memory:",
                 admin_token="admin-token")
    srv = build_server(Context(store, cfg, Monitor(store, cfg, LogNotifier())),
                       host="127.0.0.1", port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        held(store, slot_id="s1", machine="m1", account="a1")
        held(store, slot_id="s2", machine="m2", account="a2")
        tokens = {m: store.rotate_token(m) for m in ("m1", "m2")}
        for slot_id, who in (("s1", "a1"), ("s2", "a2")):
            store.request_account_action(slot_id, "use", "2", NOW + 1, held_by=who)

        def post(machine, switch):
            body = json.dumps({"node_id": machine, "mode": "machine",
                               "slots": [{"unix_user": "slot01", "present": True,
                                          "account_switch": switch}]}).encode()
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            conn.request("POST", "/api/heartbeat", body=body,
                         headers={"Authorization": f"Bearer {tokens[machine]}",
                                  "Content-Type": "application/json",
                                  "Content-Length": str(len(body))})
            reply = conn.getresponse()
            data = json.loads(reply.read())
            conn.close()
            return reply.status, data

        status, reply = post("m1", {"state": "done", "requested_at": NOW + 1})
        assert status == 200
        assert store.get_account_intent("s1") is None
        assert store.get_account_intent("s2")["state"] == "requested"
        # And the reply to m2 still asks for its own switch.
        status, reply = post("m2", None)
        assert reply["desired"]["slots"][0]["account"]["id"] == "2"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)
        store.close()


# -- nobody waits for ever ----------------------------------------------------------

def test_a_request_nobody_finished_expires_with_the_sign_ins(store):
    held(store)
    store.request_account_action("s1", "use", "2", NOW, held_by="a1")
    monitor = Monitor(store, Config(), LogNotifier(), clock=lambda: NOW + LOGIN_MAX_AGE_S - 1)
    monitor.check_all()
    assert store.get_account_intent("s1") is not None, "expired early"
    monitor = Monitor(store, Config(), LogNotifier(), clock=lambda: NOW + LOGIN_MAX_AGE_S + 1)
    monitor.check_all()
    assert store.get_account_intent("s1") is None


def test_a_late_failure_does_not_keep_the_request_past_its_window(store):
    """The window runs from the request, not from its last change: the
    privacy page promises a request is kept for at most that long, and a
    failure written near the end of it must not start the clock again."""
    held(store)
    store.request_account_action("s1", "use", "2", NOW, held_by="a1")
    store.record_account_progress("s1", {"state": "failed", "requested_at": NOW},
                                  NOW + LOGIN_MAX_AGE_S - 1)
    assert store.expire_account_intents(NOW) == 0, "gone before its window closed"
    assert store.expire_account_intents(NOW + 1) == 1, "a late failure bought a second window"
    assert store.get_account_intent("s1") is None


# -- the live database ---------------------------------------------------------------

def test_a_database_from_before_slot_accounts_opens_and_keeps_everything(tmp_path):
    """The schema exactly as main had it, frozen from sqlite_master, with rows
    like the live ones. Opening it adds what is new and changes nothing else."""
    path = str(tmp_path / "live.db")
    raw = sqlite3.connect(path)
    raw.executescript((FIXTURES / "schema-before-slot-accounts.sql").read_text())
    raw.executescript("""
        INSERT INTO nodes (id, owner, token_hash, created_at, capacity)
            VALUES ('m1', 'op', 'deadbeef', 1.0, 2);
        INSERT INTO accounts (id, google_sub, email, slot_quota, created_at)
            VALUES ('a1', 'sub-a1', 'a1@example.com', 1, 1.0);
        INSERT INTO slots (id, node_id, unix_user, state, held_by, claimed_at, present)
            VALUES ('s1', 'm1', 'slot01', 'active', 'a1', 2.0, 1);
        INSERT INTO logins (node_id, requested_at, state, url, updated_at, kind)
            VALUES ('slot:s1', 3.0, 'url_ready', 'https://claude.com/x', 3.0, 'login');
    """)
    raw.commit()
    raw.close()

    s = Store(path)
    try:
        assert s.get_slot("s1")["state"] == slots.ACTIVE
        assert s.get_slot("s1")["held_by"] == "a1"
        old = s.get_login(slot_login_key("s1"))
        assert old["state"] == "url_ready" and old["account"] == "", "an old sign-in names nothing"
        s.request_account_action("s1", "use", "2", NOW, held_by="a1")
        assert s.get_account_intent("s1")["state"] == "requested"
        s.request_slot_login("s1", "", NOW + 1, held_by="a1", account="new")
        assert s.get_login(slot_login_key("s1"))["account"] == "new"
    finally:
        s.close()
    # And again: opening twice is a no-op.
    Store(path).close()
