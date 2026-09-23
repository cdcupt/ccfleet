"""One Claude account per slot: the server's half.

The rule ccfleet keeps is one owner, one account, one node, and on a shared
machine the node is the slot. For a short while a slot could hold several of
its holder's accounts and switch between them; that broke the rule and came
out. What is left to prove:

- nothing about a second account reaches a machine, whatever an older row says;
- nothing a machine says about several accounts is kept, however many it names;
- the one account's address reaches the holder's page, whole or not at all,
  and never the console or an owner node's record;
- the page and the guide offer no way to switch, and say what to do instead;
- the database keeps the columns the feature added, so no live one migrates.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from ccfleetd import customer_docs, slots, usersite
from ccfleetd.config import Config
from ccfleetd.desired import desired_state
from ccfleetd.heartbeat import validate_heartbeat
from ccfleetd.store import Store, slot_login_key

from . import test_usersite
from .test_usersite import claimed, machine, report

# The live server and Google sign-in the other page tests use (a fixture).
site = test_usersite.site

FIXTURES = Path(__file__).parent / "fixtures"
NOW = 1_700_000_000.0
DAY = 86400
ADDRESS = "holder.one@example.org"


@pytest.fixture
def store():
    st = Store(":memory:")
    yield st
    st.close()


def active_slot(st):
    st.add_node("m1", "op", now=NOW)
    st.set_machine_capacity("m1", 1)
    st.add_slot("m1-a", "m1", "slot01", now=NOW)
    st.apply_slot_report("m1", [{"unix_user": "slot01", "present": False}], now=NOW)
    st.add_account("a1", "sub-a1", "a1@example.com", slot_quota=1, now=NOW)
    slot = st.claim_slot("a1", now=NOW)
    st.apply_slot_report("m1", [{"unix_user": "slot01", "present": True,
                                 "provisioned_for": slot["claimed_at"]}], now=NOW + 1)
    st.apply_slot_report("m1", [{"unix_user": "slot01", "present": True,
                                 "credentials": {"logged_in": True}}], now=NOW + 2)
    assert st.get_slot("m1-a")["state"] == slots.ACTIVE
    return st.get_slot("m1-a")


def raw(st, sql, *args):
    with st._lock:
        st._conn.execute(sql, args)
        st._conn.commit()


# -- what a machine is told -----------------------------------------------------------

def test_a_machine_is_never_told_about_a_second_account(store):
    """Rows the several-accounts server wrote — a request to switch, a sign-in
    aimed at "another account" — must not turn into instructions now."""
    slot = active_slot(store)
    store.request_slot_login(slot["id"], "", NOW + 3, held_by="a1")
    raw(store, "UPDATE logins SET account = 'new' WHERE node_id = ?", slot_login_key(slot["id"]))
    raw(store, "INSERT INTO account_intents (slot_id, action, account, requested_at, "
               "updated_at) VALUES (?, 'use', '2', ?, ?)", slot["id"], NOW, NOW)
    slots_ = store.list_slots(node_id="m1")
    desired = desired_state(store.get_node("m1"), None, slots_,
                            {s["id"]: store.get_login(slot_login_key(s["id"])) for s in slots_})
    [block] = desired["slots"]
    assert "account" not in block, "a switch reached the machine"
    assert set(block["login"]) == {"requested_at", "email", "kind"}, "a sign-in named a place"


def test_a_new_sign_in_clears_the_word_an_old_row_carried(store):
    slot = active_slot(store)
    store.request_slot_login(slot["id"], "", NOW + 3, held_by="a1")
    raw(store, "UPDATE logins SET account = 'new' WHERE node_id = ?", slot_login_key(slot["id"]))
    store.request_slot_login(slot["id"], "", NOW + 4, held_by="a1")
    assert store.get_login(slot_login_key(slot["id"]))["account"] == ""


# -- what a machine may say -------------------------------------------------------------

def machine_payload(entry):
    return {"node_id": "m1", "mode": "machine", "slots": [{"unix_user": "slot01", **entry}]}


@pytest.mark.parametrize("n", [1, 2, 3])
def test_a_report_of_several_accounts_is_not_kept_however_many_it_names(n):
    """The several-accounts agent's shape. The one account now arrives with the
    slot's credentials; a list of them is refused whole, one entry or three."""
    listed = [{"id": str(i), "email": f"p{i}@example.org", "active": i == 1,
               "signed_in": True} for i in range(1, n + 1)]
    kept = validate_heartbeat(machine_payload({
        "accounts": listed,
        "account_switch": {"requested_at": NOW, "state": "done", "detail": ""}}), "m1")
    [slot] = kept["slots"]
    assert "accounts" not in slot and "account_switch" not in slot
    assert "p1@example.org" not in repr(kept)


LONG_BUT_REAL = "x" * 64 + "@" + "d" * 170 + ".example.org"      # 247 characters


@pytest.mark.parametrize("address", [ADDRESS, "a@b.co", LONG_BUT_REAL])
def test_the_one_accounts_address_is_kept_whole(address):
    kept = validate_heartbeat(machine_payload({"credentials": {"email": address}}), "m1")
    assert kept["slots"][0]["credentials"]["email"] == address


@pytest.mark.parametrize("address", [
    "not an address", "x" * 250 + "@example.org", "a@b", "tab\t@example.org",
    "new\nline@example.org", 7, None, ["a@b.co"],
    # Shaped like one, but longer than SMTP allows: cut short it would be
    # somebody else's, so none of it is kept.
    "x" * 64 + "@" + "d" * 190 + ".example.org",
    # Shaped like one, with a control character the shape does not rule out.
    "bell\x07@example.org"])
def test_anything_that_is_not_an_address_is_not_kept_at_all(address):
    kept = validate_heartbeat(machine_payload({"credentials": {"email": address}}), "m1")
    assert kept["slots"][0]["credentials"]["email"] == ""


@pytest.mark.parametrize("when", ["soon", True, 10 ** 30, float("nan"), float("inf")])
def test_an_expiry_that_is_not_a_sane_number_is_not_kept(when):
    kept = validate_heartbeat(machine_payload(
        {"credentials": {"refresh_expires_at": when}}), "m1")
    assert kept["slots"][0]["credentials"]["refresh_expires_at"] is None


def test_an_owner_node_never_keeps_an_address():
    kept = validate_heartbeat({"node_id": "n1", "credentials": {
        "logged_in": True, "email": ADDRESS, "refresh_expires_at": NOW}}, "n1")
    assert "email" not in kept["credentials"]
    assert ADDRESS not in repr(kept)


# -- the holder's page ---------------------------------------------------------------

def in_use(store, browser, **credentials):
    slot = claimed(store, browser)
    report(store, "m1", [{"unix_user": slot["unix_user"], "present": True,
                          "credentials": {"logged_in": True, **credentials},
                          "remote_control": {"state": "active"}}])
    return store.get_slot(slot["id"])


def test_the_page_names_the_one_account_and_how_long_it_lasts(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    in_use(store, erik, email=ADDRESS, subscription_type="max",
           refresh_expires_at=time.time() + 28 * DAY + 60)
    page = erik.page()
    assert f"Signed in as {ADDRESS} · max plan · sign-in good for 28 more days." in page


@pytest.mark.parametrize("left, said", [(2 * DAY + 60, "good for 2 more days"),
                                        (DAY + 60, "good for 1 more day"),
                                        (3600, "ends within a day")])
def test_how_long_a_sign_in_has_left_is_said_in_words(left, said):
    now = 1_000_000.0
    assert usersite._sign_in_left(now + left, now) == said


@pytest.mark.parametrize("expires", [1_000_000.0 - 1, 1_000_000.0, True, "soon", None])
def test_a_sign_in_with_no_time_left_or_no_time_at_all_says_nothing(expires):
    assert usersite._sign_in_left(expires, 1_000_000.0) == ""


def test_an_address_on_the_page_is_escaped(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    in_use(store, erik, email="<b>x@example.org")
    page = erik.page()
    assert "<b>x@example.org" not in page and "&lt;b&gt;x@example.org" in page


def test_a_slot_in_use_whose_sign_in_is_gone_says_so_plainly(site):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = in_use(store, erik, email=ADDRESS)
    report(store, "m1", [{"unix_user": slot["unix_user"], "present": True,
                          "credentials": {"logged_in": False}}])
    page = erik.page()
    assert "Not signed in to Claude right now" in page
    assert f"Signed in as {ADDRESS}" not in page


@pytest.mark.parametrize("button", ["Add another account", "Use this one", "Remove…",
                                    "Claude accounts"])
def test_the_page_offers_no_second_account(site, button):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    in_use(store, erik, email=ADDRESS)
    assert button not in erik.page()


@pytest.mark.parametrize("action", ["add-account", "use", "forget"])
def test_the_switching_buttons_are_gone_from_the_server_too(site, action):
    store, sign_in, _ = site
    machine(store)
    erik = sign_in(quota=1)
    slot = in_use(store, erik, email=ADDRESS)
    assert action not in usersite.SLOT_ACTIONS
    assert erik.press(f"/account/slots/{slot['id']}/{action}", account="2").status == 404


# -- what the documents say ----------------------------------------------------------

def test_the_guide_says_one_account_per_slot_and_what_to_do_instead():
    guide = customer_docs.page_for("/docs/guide")(Config())
    assert "<h2>One Claude account per slot</h2>" in guide
    assert "To use another account, hold another slot." in guide
    assert "each account sees only its own machine" in guide


@pytest.mark.parametrize("path", ["/docs", "/docs/guide", "/docs/how-it-works", "/docs/terms"])
def test_no_document_offers_switching(path):
    page = customer_docs.page_for(path)(Config())
    for gone in ("Switch accounts", "Add another account", "Use this one",
                 "Up to three", "ccfleet-connect --add", "Claude accounts"):
        assert gone not in page, (path, gone)


def test_the_token_page_does_not_suggest_several_accounts_on_one_computer():
    page = usersite.token_page({"id": "m1-a"}, "sk-ant-oat01-x")
    assert "--add" not in page and "--use" not in page


# -- the live database ---------------------------------------------------------------

def test_a_database_from_before_slot_accounts_opens_and_keeps_everything(tmp_path):
    """The schema exactly as main had it before several accounts, frozen from
    sqlite_master, with rows like the live ones. Opening it adds the columns
    that feature added — kept, unused, so no live database ever migrates back —
    and changes nothing else."""
    path = str(tmp_path / "live.db")
    db = sqlite3.connect(path)
    db.executescript((FIXTURES / "schema-before-slot-accounts.sql").read_text())
    db.executescript("""
        INSERT INTO nodes (id, owner, token_hash, created_at, capacity)
            VALUES ('m1', 'op', 'deadbeef', 1.0, 2);
        INSERT INTO accounts (id, google_sub, email, slot_quota, created_at)
            VALUES ('a1', 'sub-a1', 'a1@example.com', 1, 1.0);
        INSERT INTO slots (id, node_id, unix_user, state, held_by, claimed_at, present)
            VALUES ('s1', 'm1', 'slot01', 'active', 'a1', 2.0, 1);
        INSERT INTO logins (node_id, requested_at, state, url, updated_at, kind)
            VALUES ('slot:s1', 3.0, 'url_ready', 'https://claude.com/x', 3.0, 'login');
    """)
    db.commit()
    db.close()

    s = Store(path)
    try:
        assert s.get_slot("s1")["state"] == slots.ACTIVE
        assert s.get_slot("s1")["held_by"] == "a1"
        assert s.get_login(slot_login_key("s1"))["state"] == "url_ready"
        with s._lock:
            tables = {r[0] for r in s._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            columns = {r[1] for r in s._conn.execute("PRAGMA table_info(logins)")}
        assert "account_intents" in tables and "account" in columns
    finally:
        s.close()
    Store(path).close()                     # and again: opening twice is a no-op
