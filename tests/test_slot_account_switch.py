"""A slot moves to another Claude account: the server's half.

Its holder may move a slot in use to another Claude account of theirs, at most
once a week. The server starts a change only for the slot's holder, only on a
machine's slot in use and only a week after the last one — all read in the
transaction that starts it — and starts the week only when the machine says
the account really moved. The holder's page offers it and says how it ended;
the console says that a slot changed account, never which.
"""

from __future__ import annotations

import time

import pytest

from ccfleet_agent import agent
from ccfleetd import resets, slots, usersite
from ccfleetd.desired import desired_state
from ccfleetd.store import NotYours, Store, StoreError, slot_login_key

from . import test_console_slots, test_usersite
from .test_console_slots import held_slot, row_of, slots_card, text
from .test_slot_signin import URL, held
from .test_usersite import claimed, machine, report

# The live servers the other page tests use (fixtures).
site = test_usersite.site
console = test_console_slots.console

NOW = 1_700_000_000.0
DAY = 86400
WEEK = slots.SWITCH_EVERY_S
CHANGE = 'action="/account/slots/{}/switch"'


@pytest.fixture
def store():
    st = Store(":memory:", max_slots_per_machine=8)
    yield st
    st.close()


def switched(st, at, detail=slots.SWITCHED, slot_id="s1"):
    """A change of account on the slot, asked for and reported over at `at`."""
    st.request_slot_login(slot_id, "", at, kind="switch", held_by="a1")
    st.record_login_progress(slot_login_key(slot_id), "done", "", detail, at, requested_at=at)


def slot_block(st):
    rows = st.list_slots(node_id="m1")
    [block] = desired_state(st.get_node("m1"), None, rows,
                            {s["id"]: st.get_login(slot_login_key(s["id"])) for s in rows}
                            )["slots"]
    return block


# -- who may start one, and when ---------------------------------------------------------------

def test_a_slot_in_use_can_be_moved_to_another_account(store):
    held(store)
    store.request_slot_login("s1", "", NOW + 1, kind="switch", held_by="a1")
    row = store.get_login(slot_login_key("s1"))
    assert row["state"] == "requested" and row["kind"] == "switch"
    assert slot_block(store)["login"]["kind"] == "switch", "the machine was told something else"


@pytest.mark.parametrize("state", [slots.CLAIMING, slots.CLAIMED, slots.RELEASING])
def test_only_a_slot_in_use_can_change_account(store, state):
    """A claimed slot has never been signed in: its first sign-in is the one it
    keeps, and there is nothing yet to move from."""
    held(store, state)
    with pytest.raises(StoreError):
        store.request_slot_login("s1", "", NOW + 1, kind="switch", held_by="a1")
    assert store.get_login(slot_login_key("s1")) is None


def test_only_its_holder_can_change_a_slots_account(store):
    held(store)
    with pytest.raises(NotYours):
        store.request_slot_login("s1", "", NOW + 1, kind="switch", held_by="somebody-else")
    assert store.get_login(slot_login_key("s1")) is None


def test_an_owner_node_counted_as_a_slot_cannot_change_account(store):
    """In use from the start, but its sign-in is its owner's own node's, which
    keeps no account to move from."""
    store.add_node("erik-1", "erik", now=NOW)
    store.add_account("e1", "sub-e1", "e1@example.com", slot_quota=1, now=NOW)
    slot = store.hold_owner_node("erik-1", "e1", now=NOW)
    assert slot["state"] == slots.ACTIVE
    with pytest.raises(StoreError):
        store.request_slot_login(slot["id"], "", NOW + 1, kind="switch", held_by="e1")
    assert store.get_login("erik-1") is None


def test_a_nodes_own_sign_in_is_never_a_change_of_account(store):
    store.add_node("erik-1", "erik", now=NOW)
    with pytest.raises(StoreError):
        store.request_login("erik-1", "", NOW, kind="switch")
    assert store.get_login("erik-1") is None


def test_a_slot_changes_account_at_most_once_a_week(store):
    held(store)
    switched(store, NOW + 10)
    assert store.get_slot("s1")["account_switched_at"] == NOW + 10
    with pytest.raises(StoreError):
        store.request_slot_login("s1", "", NOW + 10 + WEEK - 1, kind="switch", held_by="a1")
    assert store.get_login(slot_login_key("s1"))["state"] == "done", "a refusal changed the row"
    store.request_slot_login("s1", "", NOW + 10 + WEEK, kind="switch", held_by="a1")
    assert store.get_login(slot_login_key("s1"))["state"] == "requested"


def test_the_week_holds_back_nothing_but_a_change_of_account(store):
    held(store)
    switched(store, NOW + 10)
    store.request_slot_login("s1", "", NOW + 20, held_by="a1")
    assert store.get_login(slot_login_key("s1"))["kind"] == "login"
    store.request_slot_login("s1", "", NOW + 30, kind="token", held_by="a1")
    assert store.get_login(slot_login_key("s1"))["kind"] == "token"


# -- when the week starts, and what is kept of a change -----------------------------------------

@pytest.mark.parametrize("state,detail", [("done", slots.SAME_ACCOUNT), ("done", ""),
                                          ("failed", slots.SWITCHED)])
def test_the_week_starts_only_when_the_account_really_moved(store, state, detail):
    held(store)
    store.request_slot_login("s1", "", NOW + 1, kind="switch", held_by="a1")
    store.record_login_progress(slot_login_key("s1"), state, "", detail, NOW + 2,
                                requested_at=NOW + 1)
    assert store.get_slot("s1")["account_switched_at"] is None
    store.request_slot_login("s1", "", NOW + 3, kind="switch", held_by="a1")


def test_a_sign_in_that_claims_to_have_switched_starts_no_week(store):
    """Only a change of account may move a slot to another account, so a plain
    sign-in's done means nothing more, whatever words come with it."""
    held(store)
    store.request_slot_login("s1", "", NOW + 1, held_by="a1")
    store.record_login_progress(slot_login_key("s1"), "done", "", slots.SWITCHED, NOW + 2,
                                requested_at=NOW + 1)
    assert store.get_slot("s1")["account_switched_at"] is None
    assert store.get_login(slot_login_key("s1")) is None


@pytest.mark.parametrize("detail", slots.SWITCH_ENDINGS)
def test_how_a_change_ended_stays_for_its_holder_and_nothing_else(store, detail):
    held(store)
    key = slot_login_key("s1")
    store.request_slot_login("s1", "", NOW + 1, kind="switch", held_by="a1")
    store.record_login_progress(key, "url_ready", URL, "", NOW + 2, requested_at=NOW + 1)
    store.submit_slot_login_code("s1", "the-code", NOW + 3, held_by="a1")
    store.record_login_progress(key, "done", "", f" {detail} ", NOW + 4, requested_at=NOW + 1)
    row = store.get_login(key)
    assert row["state"] == "done" and row["detail"] == detail
    assert row["code"] == "" and row["url"] == "" and row["secret"] == ""


def test_a_change_that_ends_without_its_word_goes_like_any_sign_in(store):
    held(store)
    switched(store, NOW + 1, detail="")
    assert store.get_login(slot_login_key("s1")) is None


def test_a_finished_change_is_never_asked_of_the_machine_again(store):
    held(store)
    switched(store, NOW + 10)
    assert "login" not in slot_block(store)


def test_the_week_goes_with_the_slot_when_it_is_freed(store):
    held(store)
    switched(store, NOW + 10)
    store.begin_release("s1")
    store.apply_slot_report("m1", [{"unix_user": "slot01", "present": False}], now=NOW + 20)
    freed = store.get_slot("s1")
    assert freed["state"] == slots.FREE and freed["account_switched_at"] is None


def test_the_machine_and_the_server_say_how_a_change_ended_in_the_same_words():
    assert (agent.SWITCHED, agent.SAME_ACCOUNT) == slots.SWITCH_ENDINGS


# -- the holder's page -------------------------------------------------------------------------

def in_use(store, browser):
    """A slot of theirs, set up and reported signed in."""
    slot = claimed(store, browser)
    report(store, "m1", [{"unix_user": slot["unix_user"], "present": True,
                          "credentials": {"logged_in": True},
                          "remote_control": {"state": "active"}}])
    slot = store.get_slot(slot["id"])
    assert slot["state"] == slots.ACTIVE
    return slot


def ended(store, slot, detail):
    """Their page's change of account, reported over by the machine."""
    key = slot_login_key(slot["id"])
    store.record_login_progress(key, "done", "", detail, time.time(),
                                store.get_login(key)["requested_at"])


def test_a_slot_in_use_offers_change_account_beside_sign_in_again(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = in_use(store, erik)
    page = erik.page()
    assert CHANGE.format(slot["id"]) in page and ">Change account</button>" in page
    assert "Sign in again" in page


def test_a_slot_not_in_use_offers_no_change_and_refuses_one(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = claimed(store, erik)
    assert CHANGE.format(slot["id"]) not in erik.page()
    reply = erik.press(f"/account/slots/{slot['id']}/switch")
    assert reply.getheader("Location").startswith("/account?note=not-now")
    assert store.get_login(slot_login_key(slot["id"])) is None


def test_changing_account_from_the_page(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = in_use(store, erik)
    key = slot_login_key(slot["id"])
    reply = erik.press(f"/account/slots/{slot['id']}/switch")
    assert reply.getheader("Location").startswith("/account?note=switch")
    row = store.get_login(key)
    assert row["state"] == "requested" and row["kind"] == "switch"
    page = erik.page()
    assert usersite.SWITCH_FLOW in page and "Asking the node" in page
    assert CHANGE.format(slot["id"]) not in page, "offered again while one runs"
    store.record_login_progress(key, "url_ready", URL, "", time.time(), row["requested_at"])
    page = erik.page()
    assert f'href="{URL.replace("&", "&amp;")}"' in page and 'name="code"' in page
    assert usersite.SWITCH_FLOW in page
    erik.press(f"/account/slots/{slot['id']}/code", code="the-code")
    assert store.get_login(key)["state"] == "code_sent"


def test_for_a_week_after_a_change_the_card_says_when_it_can_change_again(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = in_use(store, erik)
    erik.press(f"/account/slots/{slot['id']}/switch")
    ended(store, slot, slots.SWITCHED)
    at = store.get_slot(slot["id"])["account_switched_at"]
    page = erik.page()
    assert "Your slot now uses the new Claude account." in page
    assert CHANGE.format(slot["id"]) not in page
    assert (f'You can change account again <time datetime="{resets.iso(at + WEEK)}" '
            'data-local>in 6d 23h</time>') in page
    reply = erik.press(f"/account/slots/{slot['id']}/switch")
    assert reply.getheader("Location").startswith("/account?note=not-now")
    assert store.get_login(slot_login_key(slot["id"]))["state"] == "done"


def test_a_change_to_the_same_account_says_so_and_can_be_made_again(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = in_use(store, erik)
    erik.press(f"/account/slots/{slot['id']}/switch")
    ended(store, slot, slots.SAME_ACCOUNT)
    page = erik.page()
    assert "That is the account this slot already had; nothing changed." in page
    assert CHANGE.format(slot["id"]) in page and "Get a device token" in page


def test_a_finished_row_with_any_other_word_says_nothing(site):
    """Only the two fixed words are ever kept; a row that says anything else,
    however it came to be there, is not put on the page."""
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = in_use(store, erik)
    erik.press(f"/account/slots/{slot['id']}/switch")
    with store._lock:                     # as if written some other way
        store._conn.execute("UPDATE logins SET state = 'done', detail = ? WHERE node_id = ?",
                            ("<b>zq-moved</b>", slot_login_key(slot["id"])))
        store._conn.commit()
    page = erik.page()
    assert "zq-moved" not in page and '<p class="">' not in page
    assert CHANGE.format(slot["id"]) in page


def test_a_change_that_did_not_go_through_says_so(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = in_use(store, erik)
    key = slot_login_key(slot["id"])
    erik.press(f"/account/slots/{slot['id']}/switch")
    store.record_login_progress(key, "failed", "", agent.NOT_ADOPTED, time.time(),
                                store.get_login(key)["requested_at"])
    page = erik.page()
    assert f"The account was not changed: {agent.NOT_ADOPTED}." in page
    assert CHANGE.format(slot["id"]) in page, "a failed change started the week"


def test_somebody_elses_slot_in_use_cannot_be_changed(site):
    store, sign_in, _ = site
    machine(store)
    ana = sign_in("google-ana", "ana@example.com", quota=1)
    theirs = in_use(store, ana)
    erik = sign_in(quota=1)
    assert erik.press(f"/account/slots/{theirs['id']}/switch").status == 404
    assert store.get_login(slot_login_key(theirs["id"])) is None


@pytest.mark.parametrize("token", ["", "0" * 64])
def test_a_change_of_account_needs_this_sessions_token(site, token):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = in_use(store, erik)
    reply = erik.call("POST", f"/account/slots/{slot['id']}/switch",
                      form={"csrf": token} if token else {})
    assert reply.status == 403
    assert store.get_login(slot_login_key(slot["id"])) is None


def test_an_owner_node_counted_as_a_slot_offers_no_change(site):
    store, sign_in, _ = site
    erik = sign_in(quota=1)
    store.add_node("erik-1", "erik", now=time.time())
    slot = store.hold_owner_node("erik-1", erik.account["id"], now=time.time())
    store.insert_heartbeat("erik-1", time.time(),
                           {"node_id": "erik-1", "credentials": {"logged_in": True}})
    page = erik.page()
    assert "Sign in again" in page
    assert CHANGE.format(slot["id"]) not in page


# -- the console -------------------------------------------------------------------------------

def changed_at(store, at):
    """The console's one held slot, in use, moved to another account at `at`."""
    slot = held_slot(store)
    store.apply_slot_report("m1", [{"unix_user": "slot01", "present": True,
                                    "credentials": {"logged_in": True}}], now=time.time())
    store.request_slot_login(slot["id"], "", at, kind="switch")
    store.record_login_progress(slot_login_key(slot["id"]), "done", "", slots.SWITCHED, at,
                                requested_at=at)


def test_the_console_says_a_slot_changed_account_for_the_week_after(console):
    store, call = console
    changed_at(store, time.time() - 2 * DAY)
    row = text(row_of(slots_card(call("GET", "/admin").body), "m1"))
    assert "changed Claude account 2.0d ago" in row


def test_a_change_older_than_a_week_is_no_longer_said(console):
    store, call = console
    changed_at(store, time.time() - WEEK - 60)
    assert "changed Claude account" not in slots_card(call("GET", "/admin").body)
